from http import HTTPStatus
from json import JSONDecodeError

import httpx
import pytest
from starlette.testclient import TestClient

from main import app
from src.models import LinkRequest

test_websites = [
    "https://ext.to/",
    # "https://www.ygg.re/",
    "https://extratorrent.st/",
    "https://speed.cd/login",
    'https://www.yggtorrent.top/engine/search?do=search&order=desc&sort=publish_date&name="UNESCAPED"+"DOUBLEQUOTES"&category=2145',
    "https://1337x.to/home/",
]


@pytest.fixture
def client():
    """Create a test client with proper lifespan handling for browser initialization."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("website", test_websites)
def test_bypass(client: TestClient, website: str):
    """
    Tests if the service can bypass cloudflare/DDOS-GUARD on given websites.

    This test is skipped if the website is not reachable or does not have cloudflare/DDOS-GUARD.
    """
    test_request = httpx.get(
        website,
    )
    if (
        test_request.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
        and "Just a moment..." not in test_request.text
    ):
        try:
            error_details = test_request.json()
        except JSONDecodeError:
            error_details = test_request.text
        pytest.skip(
            f"Skipping {website} - ({test_request.status_code}) {error_details}"
        )

    response = client.post(
        "/v1",
        json=LinkRequest.model_construct(url=website, cmd="request.get").model_dump(),
    )

    assert response.status_code == HTTPStatus.OK


def test_health_check(client: TestClient):
    """
    Tests the health check endpoint.

    This test ensures that the health check endpoint returns HTTPStatus.OK
    and the response body contains the expected fields.
    """
    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert "userAgent" in data
    assert "version" in data
    assert data["msg"] == "Byparr is working!"
