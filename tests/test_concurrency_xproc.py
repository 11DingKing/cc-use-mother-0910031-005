"""跨进程并发确认测试：验证 fcntl 文件锁在进程级别也只放行一次确认。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


WORKER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {src!r})
    from credit_service import CreditService, Store
    svc = CreditService(Store({data_dir!r}))
    try:
        svc.confirm("filing-E1-2025")
        print("ok")
    except Exception as exc:
        print(type(exc).__name__)
""")


class CrossProcessConfirmTest(unittest.TestCase):
    def test_only_one_process_confirms(self) -> None:
        data_dir = tempfile.mkdtemp(prefix="credit-xproc-")
        seed = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT / 'src')!r})
            from credit_service import CreditService, Store
            svc = CreditService(Store({data_dir!r}))
            ev = svc.register_evidence(source="s", batch="b", reference="r", payload={{}})
            svc.add_rule_version(year=2025, policy_coef="3.0", rate="0.12", evidence=ev)
            svc.add_model_version(model_id="M-A", enterprise_id="E1", name="A",
                                  year=2025, volume=10, energy="6.0", evidence=ev)
            svc.open_filing("E1", 2025)
            svc.trial("filing-E1-2025", model_versions={{"M-A": 1}})
        """)
        subprocess.run([sys.executable, "-c", seed], check=True)

        procs = [
            subprocess.Popen(
                [sys.executable, "-c",
                 WORKER.format(src=str(ROOT / "src"), data_dir=data_dir)],
                stdout=subprocess.PIPE, text=True)
            for _ in range(4)
        ]
        outputs = sorted(p.communicate()[0].strip() for p in procs)
        self.assertEqual(outputs.count("ok"), 1, outputs)
        self.assertEqual(outputs.count("ConflictError"), 3, outputs)

        # 原始分录恰好一条，无重复
        verify = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT / 'src')!r})
            from credit_service import CreditService, Store
            svc = CreditService(Store({data_dir!r}))
            entries = svc.store.list_entries("filing-E1-2025")
            assert len(entries) == 1, entries
            result = svc.reverify("filing-E1-2025")
            assert result["ledger_match"], result
            print("verified")
        """)
        out = subprocess.run([sys.executable, "-c", verify], check=True,
                             capture_output=True, text=True)
        self.assertIn("verified", out.stdout)


if __name__ == "__main__":
    unittest.main()
