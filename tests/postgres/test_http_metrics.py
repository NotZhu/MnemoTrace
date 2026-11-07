"""The HTTP metrics response reports only aggregates."""
from tests.postgres.test_http_product import hdr


def test_http_metrics_endpoint(product_client):
    headers = hdr("synthetic-metrics")
    result = product_client.post("/v1/remember", headers=headers, json={"content": "I work at Acme."})
    assert result.status_code == 200
    payload = product_client.get("/metrics", headers=headers).json()
    assert "Acme" not in str(payload)
    assert "remember" in payload["ops"]
