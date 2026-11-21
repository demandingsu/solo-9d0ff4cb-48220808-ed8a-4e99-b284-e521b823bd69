"""并发补报测试：串行化事务保证不丢失不相容检查。"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

def test_concurrent_conflicting_reports_only_one_wins(tmp_path):
    app = create_app(str(tmp_path / "concurrent.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def submit(count: int):
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": 0, "hi": 10, "count": count}]},
            ).status_code

        # 5 与 6 互相矛盾，只能有一个成功。
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(submit, [5, 6]))

        assert statuses == [201, 409]
        detail = client.get("/inspections/1").json()
        assert detail["report_count"] == 1
        assert detail["segment_count"] == 1


def test_concurrent_compatible_reports_all_accepted(tmp_path):
    app = create_app(str(tmp_path / "concurrent2.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def submit(pair):
            lo, hi = pair
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": lo, "hi": hi, "count": 2}]},
            ).status_code

        pairs = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            statuses = list(pool.map(submit, pairs))

        assert statuses == [201] * 5
        detail = client.get("/inspections/1").json()
        assert detail["report_count"] == 5
        bounds = client.get(
            "/inspections/1/bounds", params={"qualified_lo": 0, "qualified_hi": 10}
        ).json()
        assert bounds["min_count"] == 10
        assert bounds["max_count"] == 10


def test_correction_concurrent_with_report_and_issuance(tmp_path):
    """更正事务与补报、开证并发：全部串行化，最终状态自洽且无丢失。"""
    app = create_app(str(tmp_path / "concurrent_cert.db"))
    with TestClient(app) as client:
        # 总片数 100；重叠补报后交集 [50,60) 下界 30。
        assert client.post("/inspections", json={"total_pieces": 100}).status_code == 201
        for segment in ({"lo": 40, "hi": 60, "count": 70}, {"lo": 50, "hi": 70, "count": 60}):
            assert (
                client.post(
                    "/inspections/1/reports", json={"segments": [segment]}
                ).status_code
                == 201
            )

        def correct():
            # 120 与现有计数相容；交集下界降为 10。
            return client.patch(
                "/inspections/1", json={"total_pieces": 120}
            ).status_code

        def add_report():
            # 与两种总片数都相容的补报。
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": 0, "hi": 10, "count": 5}]},
            ).status_code

        def issue_cert():
            return client.post(
                "/inspections/1/certificates",
                json={
                    "qualified_lo": 50,
                    "qualified_hi": 60,
                    "committed_count": 30,
                },
            ).status_code

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(
                pool.map(lambda fn: fn(), [correct, add_report, issue_cert])
            )
        issue_status = results[-1]

        # 更正与补报必然成功；开证结果取决于它在更正提交前还是后获得锁：
        #   更正前：当时下界 >= 30 => 201，随后被同事务更正撤销（终态 revoked）；
        #   更正后：下界低于 30 => 409，不写入证明。
        # 补报与更正的先后同样不固定，因此快照只断言合法取值集合。
        assert 200 in results
        assert results.count(201) >= 1  # 补报必成功；开证成功时另有一个 201
        assert issue_status in (201, 409)

        detail = client.get("/inspections/1").json()
        assert detail["total_pieces"] == 120
        assert detail["report_count"] == 3  # 补报没有丢失

        certificates = client.get("/inspections/1/certificates").json()
        if issue_status == 409:
            assert certificates == []
        else:
            assert len(certificates) == 1
            certificate = certificates[0]
            # 开证在更正前，因此更正生效后必须按新下界撤销承诺 30 的证明。
            assert certificate["status"] == "revoked"
            assert certificate["committed_count"] == 30
            assert certificate["revoked_total_pieces"] == 120
            # 撤销下界取决于补报是否已在更正事务前入库：15（已入库）或 10（未入库）。
            assert certificate["revoked_min_count"] in (10, 15)
            # 已撤销证明仍可查原承诺与开证快照；开证时补报是否已入库
            # 决定开证下界快照为 30（补报在后）或 35（补报在前）。
            assert certificate["issued_total_pieces"] == 100
            assert certificate["issued_min_count"] in (30, 35)
        assert (
            detail["active_certificate_count"] + detail["revoked_certificate_count"]
            == len(certificates)
        )


def test_concurrent_issuance_keeps_all_certificates(tmp_path):
    """并发开证互不覆盖，证明编号唯一且全部可凭号核验。"""
    app = create_app(str(tmp_path / "concurrent_issue.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def issue(_):
            return client.post(
                "/inspections/1/certificates",
                json={"qualified_lo": 0, "qualified_hi": 5, "committed_count": 0},
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(issue, range(8)))

        assert all(response.status_code == 201 for response in responses)
        numbers = sorted(response.json()["certificate_no"] for response in responses)
        assert len(set(numbers)) == 8
        for number in numbers:
            assert client.get(f"/certificates/{number}").status_code == 200
        assert client.get("/inspections/1").json()["active_certificate_count"] == 8
