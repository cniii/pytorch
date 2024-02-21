# Owner(s): ["module: inductor"]
import math
import os
import sys

import torch
from torch._inductor.utils import run_and_get_code
from torch.testing import FileCheck
from torch.testing._internal.common_utils import (
    IS_CI,
    IS_WINDOWS,
    skipIfRocm,
    slowTest,
    TEST_WITH_ASAN,
    TestCase as TorchTestCase,
)

from torch.testing._internal.inductor_utils import HAS_CPU, HAS_CUDA

# Make the helper files in test/ importable
pytorch_test_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(pytorch_test_dir)

import contextlib
import unittest

from torch._inductor import config
from torch._inductor.scheduler import Scheduler


if IS_WINDOWS and IS_CI:
    sys.stderr.write(
        "Windows CI does not have necessary dependencies for test_torchinductor yet\n"
    )
    if __name__ == "__main__":
        sys.exit(0)
    raise unittest.SkipTest("requires sympy/functorch/filelock")

from torch._inductor.select_algorithm import ExternKernelCaller, TritonTemplateCaller

from inductor.test_torchinductor import check_model, check_model_cuda, copy_tests


class TestCase(TorchTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._stack = contextlib.ExitStack()
        cls._stack.enter_context(
            config.patch(
                {
                    "benchmark_kernel": True,
                    "benchmark_fusion": True,
                }
            )
        )

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()
        super().tearDownClass()


def filter_extern(choice):
    return isinstance(choice, ExternKernelCaller)


class BenchmarkFusionTestTemplate:
    def test_softmax(self):
        def f(x):
            return torch.nn.functional.softmax(x, dim=-1)

        self.common(f, (torch.rand(2, 8192),))

    @slowTest
    @skipIfRocm  # fail accuracy check on ROCm
    def test_resnet18(self):
        import torchvision

        model = torchvision.models.resnet18()
        model.eval()
        batch_size = 16
        inputs = (torch.randn((batch_size, 3, 224, 224)),)
        self.common(model, inputs, atol=1e-2, rtol=1e-2)

    def test_register_spills(self):
        """
        The test can potentially trigger register spills
        """
        old_benchmark_fn = Scheduler.benchmark_fused_nodes

        def new_benchmark_fn(scheduler, nodes):
            """
            We override Scheduler.benchmark_fused_nodes to return latency 1.0
            if there are no register spills. Without this, we may not able to
            test the code path handling register spilling because before register
            start spilling, the related fusion may have already been skipped
            due to longer lantency.
            """
            ms, path = old_benchmark_fn(scheduler, nodes)
            if not math.isinf(ms):
                ms = 1.0
            return ms, path

        # Disable dynamic_scale_rblock to make it easier to trigger register
        # spilling.
        with unittest.mock.patch.object(
            Scheduler, "benchmark_fused_nodes", new_benchmark_fn
        ), config.patch("dynamic_scale_rblock", False):
            S = 512

            def f(*inputs):
                inputs = list(inputs)
                outputs = []
                out = torch.zeros(S, device=self.device)
                for x in inputs:
                    x = x * 2
                    x = x + 1
                    x = x.sum(dim=-1)
                    outputs.append(x)
                    out = out + x
                return outputs, out

            N = int(os.environ.get("NINP", "30"))
            inputs = [torch.randn(S, 2560, device=self.device) for _ in range(N)]
            opt_f = torch.compile(f)
            opt_f(*inputs)

    @torch._inductor.config.patch(max_autotune_gemm_backends="TRITON")
    def test_avoid_register_spilling(self):
        if self.device != "cuda":
            raise unittest.SkipTest("CUDA only")

        from torch.nn.functional import gelu

        def foo(m, inp):
            curr = m(inp)
            tmps = []
            for _ in range(10):
                curr = gelu(curr)
                for t in tmps:
                    curr = curr + t
                tmps.append(curr)

            return curr

        foo_c = torch.compile(mode="max-autotune-no-cudagraphs")(foo)

        with torch.no_grad():
            m = torch.nn.Linear(2048, 2048, bias=True).half().cuda()
            inp = torch.rand([2048, 2048]).half().cuda()

            foo_c(m, inp)

            _, out_code = run_and_get_code(foo_c, m, inp)

            # should be multiple triton invocations
            FileCheck().check("async_compile.wait").check_count(
                ".run", 2, exactly=True
            ).run(out_code[0])


if HAS_CUDA and not TEST_WITH_ASAN:

    class BenchmarkFusionCudaTest(TestCase):
        common = check_model_cuda
        device = "cuda"

    copy_tests(BenchmarkFusionTestTemplate, BenchmarkFusionCudaTest, "cuda")

    class BenchmarkMultiTemplateFusionCudaTest(TorchTestCase):
        @classmethod
        def setUpClass(cls):
            super().setUpClass()
            cls._stack = contextlib.ExitStack()
            cls._stack.enter_context(
                config.patch(
                    {
                        "benchmark_kernel": True,
                        "benchmark_fusion": True,
                        "benchmark_multi_templates": True,
                    }
                )
            )

        @classmethod
        def tearDownClass(cls):
            cls._stack.close()
            super().tearDownClass()

        def _equivalent_output_code_impl(self):
            def foo(m, inp):
                a = m(inp)
                return torch.nn.functional.relu(a)

            foo_c = torch.compile(mode="max-autotune-no-cudagraphs")(foo)

            m = torch.nn.Linear(512, 512, bias=True).half().cuda()
            inp = torch.rand([512, 512]).half().cuda()

            with torch.no_grad():
                res, code = run_and_get_code(foo_c, m, inp)

            torch._dynamo.reset()
            with unittest.mock.patch.object(
                torch._inductor.config, "benchmark_multi_templates", False
            ):
                foo_c = torch.compile(mode="max-autotune-no-cudagraphs")(foo)
                with torch.no_grad():
                    res2, code2 = run_and_get_code(foo_c, m, inp)

            self.assertEqual(res, res2, atol=1e-4, rtol=1.1)
            return code, code2

        @torch._inductor.config.patch(max_autotune_gemm_backends="TRITON")
        def test_equivalent_template_code(self):
            code, code2 = self._equivalent_output_code_impl()
            for out_code in [code, code2]:
                FileCheck().check("def call").check_count(
                    "empty_strided_cuda", 1, exactly=True
                ).check("triton_tem_fused_relu_0.run").check_count(
                    "del", 3, exactly=True
                ).check(
                    "return"
                ).run(
                    out_code[0]
                )

        @torch._inductor.config.patch(debug_filter_choice=filter_extern)
        def test_equivalent_extern_code(self):
            torch._dynamo.reset()

            code, code2 = self._equivalent_output_code_impl()

            for out_code in [code, code2]:
                FileCheck().check("def call").check_count(
                    "empty_strided_cuda", 1, exactly=True
                ).check("extern_kernels.mm").check_count("del", 3, exactly=True).check(
                    "reuse"
                ).check(
                    "return"
                ).run(
                    out_code[0]
                )

        def test_changed_layout(self):
            # cat addmm planning will change layout - make sure propagated

            for allowed_type in [ExternKernelCaller, TritonTemplateCaller]:

                def fn(a: torch.Tensor, b: torch.Tensor, c: torch.Tensor):
                    return torch.cat(
                        [
                            torch.addmm(a, b, c),
                            torch.addmm(b, c, a),
                        ],
                        1,
                    )

                args = [
                    torch.randn(4, 4, device="cuda"),
                    torch.randn(4, 4, device="cuda"),
                    torch.randn(4, 4, device="cuda"),
                ]

                def filter_choice(choice):
                    return isinstance(choice, allowed_type)

                with config.patch("debug_filter_choice", filter_choice):
                    expected = fn(*args)
                    actual = torch.compile(fn, mode="max-autotune")(*args)
                    self.assertEqual(expected, actual)

                torch._dynamo.reset()


if HAS_CPU and not torch.backends.mps.is_available():

    class BenchmarkFusionCpuTest(TestCase):
        common = check_model
        device = "cpu"

    copy_tests(BenchmarkFusionTestTemplate, BenchmarkFusionCpuTest, "cpu")

if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    if HAS_CPU or HAS_CUDA:
        run_tests()
