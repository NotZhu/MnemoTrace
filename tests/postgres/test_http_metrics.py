"""The HTTP metrics response reports only aggregates."""
from tests.postgres.test_http_product import hdr


def test_http_metrics_endpoint(product_client):
    headers = hdr("synthetic-metrics")
    result = product_client.publish_text("I work at Acme.", headers=headers)
    assert result.status_code == 200
    payload = product_client.get("/metrics", headers=headers).json()
    assert "Acme" not in str(payload)
    assert "remember" in payload["ops"]
