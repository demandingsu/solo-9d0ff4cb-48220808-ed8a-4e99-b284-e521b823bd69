"""证明 API 测试：开证、明号核验、更正撤销、终态与原子性。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def make_inspection(client, total_pieces):
    response = client.post("/inspections", json={"total_pieces": total_pieces})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def issue(client, inspection_id, lo, hi, committed):
    return client.post(
        f"/inspections/{inspection_id}/certificates",
        json={
            "qualified_lo": lo,
            "qualified_hi": hi,
            "committed_count": committed,
        },
    )


def test_issue_certificate_when_lower_bound_meets_commitment(client):
    make_inspection(client, 100)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 40, "hi": 60, "count": 70}]},
        ).status_code
        == 201
    )

    # [40,60) 被精确约束为 70 片，承诺 70 可开证。
    response = issue(client, 1, 40, 60, 70)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["certificate_no"] == "CERT-000001"
    assert body["status"] == "active"
    assert body["committed_count"] == 70
    assert body["issued_total_pieces"] == 100
    assert body["issued_min_count"] == 70
    assert body["revoked_at"] is None
    assert body["current_total_pieces"] == 100
    assert body["current_min_count"] == 70
    assert body["current_max_count"] == 70

    # 登记摘要里的证明计数随之更新。
    summary = client.get("/inspections/1").json()
    assert summary["active_certificate_count"] == 1
    assert summary["revoked_certificate_count"] == 0


def test_issue_rejected_when_lower_bound_below_commitment(client):
    make_inspection(client, 100)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 40, "hi": 60, "count": 70}]},
        ).status_code
        == 201
    )

    # 无约束把片数钉在 [10,20)，下界为 0；承诺 1 不予开证。
    response = issue(client, 1, 10, 20, 1)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "下界为 0" in detail
    assert "未达到承诺片数 1" in detail

    # 拒绝不写入任何证明。
    assert client.get("/inspections/1/certificates").json() == []
    assert client.get("/inspections/1").json()["active_certificate_count"] == 0

    # 承诺 0 总可开证（下界 >= 0）。
    ok = issue(client, 1, 10, 20, 0)
    assert ok.status_code == 201
    assert ok.json()["certificate_no"] == "CERT-000001"


def test_certificate_validation_errors(client):
    make_inspection(client, 10)
    # hi <= lo
    assert issue(client, 1, 10, 10, 1).status_code == 422
    # 负承诺
    assert issue(client, 1, 0, 10, -1).status_code == 422
    # 不存在的质检号
    assert issue(client, 99, 0, 10, 0).status_code == 404
    assert client.get("/inspections/99/certificates").status_code == 404


def test_certificate_number_lookup_for_customer(client):
    make_inspection(client, 100)
    issued = issue(client, 1, 0, 50, 0).json()
    number = issued["certificate_no"]

    found = client.get(f"/certificates/{number}")
    assert found.status_code == 200
    assert found.json()["certificate_no"] == number
    assert found.json()["inspection_id"] == 1

    # 编号格式错误或不存在都按 404 处理，避免猜测有效编号。
    assert client.get("/certificates/NOPE-1").status_code == 404
    assert client.get("/certificates/CERT-999999").status_code == 404


def test_correction_revokes_certificates_under_new_lower_bound(client):
    make_inspection(client, 100)
    # 重叠补报：[40,60)=70、[50,70)=60，交集 [50,60) 下界 30、上界 60。
    for segment in ({"lo": 40, "hi": 60, "count": 70}, {"lo": 50, "hi": 70, "count": 60}):
        assert (
            client.post(
                "/inspections/1/reports", json={"segments": [segment]}
            ).status_code
            == 201
        )

    high = issue(client, 1, 50, 60, 30)
    assert high.status_code == 201
    high_no = high.json()["certificate_no"]
    low = issue(client, 1, 50, 60, 10)
    assert low.status_code == 201
    low_no = low.json()["certificate_no"]

    # 更正总片数 100 -> 120：与计数相容；交集下界由 30 降为 10。
    patch = client.patch("/inspections/1", json={"total_pieces": 120})
    assert patch.status_code == 200
    assert patch.json()["total_pieces"] == 120
    assert patch.json()["active_certificate_count"] == 1
    assert patch.json()["revoked_certificate_count"] == 1

    # 承诺 30 的证明：新下界 10 < 30，被撤销。
    revoked = client.get(f"/certificates/{high_no}").json()
    assert revoked["status"] == "revoked"
    assert revoked["committed_count"] == 30            # 原承诺仍可查
    assert revoked["issued_total_pieces"] == 100       # 开证时总片数
    assert revoked["issued_min_count"] == 30           # 开证时下界
    assert revoked["revoked_total_pieces"] == 120      # 更正后的总片数
    assert revoked["revoked_min_count"] == 10          # 撤销时的下界
    assert revoked["current_total_pieces"] == 120
    assert revoked["current_min_count"] is None
    assert revoked["current_max_count"] is None
    assert "10" in revoked["revoked_reason"]
    assert revoked["revoked_at"] is not None

    # 承诺 10 的证明仍有效，并反映新数据。
    active = client.get(f"/certificates/{low_no}").json()
    assert active["status"] == "active"
    assert active["current_total_pieces"] == 120
    assert active["current_min_count"] == 10
    assert active["revoked_at"] is None


def test_revocation_is_terminal_never_restored(client):
    make_inspection(client, 100)
    for segment in ({"lo": 40, "hi": 60, "count": 70}, {"lo": 50, "hi": 70, "count": 60}):
        assert (
            client.post(
                "/inspections/1/reports", json={"segments": [segment]}
            ).status_code
            == 201
        )
    number = issue(client, 1, 50, 60, 30).json()["certificate_no"]

    # 更正到 120（下界 30 -> 10）触发撤销。
    assert client.patch("/inspections/1", json={"total_pieces": 120}).status_code == 200
    assert client.get(f"/certificates/{number}").json()["status"] == "revoked"

    # 再更正回 100，下界虽恢复为 30，已撤销证明也不复活。
    assert client.patch("/inspections/1", json={"total_pieces": 100}).status_code == 200
    record = client.get(f"/certificates/{number}").json()
    assert record["status"] == "revoked"
    # 撤销快照保持第一次撤销时的值，不被后续更正改写。
    assert record["revoked_total_pieces"] == 120
    assert record["revoked_min_count"] == 10
    assert record["current_total_pieces"] == 120

    # 列表接口同样不把它计为有效。
    listed = client.get("/inspections/1/certificates").json()
    assert [c["status"] for c in listed] == ["revoked"]
    summary = client.get("/inspections/1").json()
    assert summary["active_certificate_count"] == 0
    assert summary["revoked_certificate_count"] == 1


def test_failed_correction_changes_nothing_and_remains_queryable(client):
    make_inspection(client, 100)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 0, "hi": 50, "count": 60}]},
        ).status_code
        == 201
    )
    number = issue(client, 1, 0, 50, 60).json()["certificate_no"]

    # 50 < 已报 60，相容性校验失败：总片数与证明状态都不变。
    conflict = client.patch("/inspections/1", json={"total_pieces": 50})
    assert conflict.status_code == 409
    detail = client.get("/inspections/1").json()
    assert detail["total_pieces"] == 100
    assert detail["active_certificate_count"] == 1

    certificate = client.get(f"/certificates/{number}").json()
    assert certificate["status"] == "active"
    assert certificate["current_total_pieces"] == 100
    assert certificate["current_min_count"] == 60

    # 原 bounds 结果仍可查。
    bounds = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 0, "qualified_hi": 50}
    ).json()
    assert bounds["min_count"] == 60


def test_note_only_patch_does_not_revoke(client):
    make_inspection(client, 100)
    number = issue(client, 1, 0, 50, 0).json()["certificate_no"]
    response = client.patch("/inspections/1", json={"note": "复核备注"})
    assert response.status_code == 200
    assert client.get(f"/certificates/{number}").json()["status"] == "active"


def test_reports_and_issuance_continue_between_corrections(client):
    """试算期间同批可继续补报和开证，更正生效时纳入此前全部提交。"""
    make_inspection(client, 100)
    # 先开一份基于空补报的证明：[50,60) 下界 0，承诺 0。
    first_no = issue(client, 1, 50, 60, 0).json()["certificate_no"]

    # 试算期间继续补报：重叠区间使交集 [50,60) 下界为 30。
    for segment in ({"lo": 40, "hi": 60, "count": 70}, {"lo": 50, "hi": 70, "count": 60}):
        assert (
            client.post(
                "/inspections/1/reports", json={"segments": [segment]}
            ).status_code
            == 201
        )
    # 同批继续开证：此时下界 30，可承诺 30。
    second_no = issue(client, 1, 50, 60, 30).json()["certificate_no"]

    # 更正总片数到 120（相容）：交集下界降为 10，承诺 30 的证明被撤销，
    # 而承诺 0 的证明继续有效——证明更正确实纳入了此前的补报与证明。
    assert client.patch("/inspections/1", json={"total_pieces": 120}).status_code == 200
    assert client.get(f"/certificates/{first_no}").json()["status"] == "active"
    second = client.get(f"/certificates/{second_no}").json()
    assert second["status"] == "revoked"
    assert second["revoked_min_count"] == 10
    assert second["revoked_total_pieces"] == 120


def test_certificate_numbers_are_unique_and_stable(client):
    make_inspection(client, 10)
    first = issue(client, 1, 0, 5, 0).json()["certificate_no"]
    second = issue(client, 1, 0, 5, 0).json()["certificate_no"]
    assert first == "CERT-000001"
    assert second == "CERT-000002"
    # 撤销后编号保持不变。
    client.patch("/inspections/1", json={"total_pieces": 10, "note": "no-op"})
    assert client.get(f"/certificates/{first}").json()["certificate_no"] == first
