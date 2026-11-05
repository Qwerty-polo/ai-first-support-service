import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.errors import PermanentError, RetryableError
from app.knowledge import ARTICLES, search_articles
from app.llm import Candidate, OpenAIAdapter, validate_sources
from app.main import create_app


def test_search_finds_pdf_and_does_not_invent_matches() -> None:
    assert search_articles("Як відновити пароль?")[0].id == "reset-password"
    assert search_articles("Не можу експортувати звіт у PDF")[0].id == "export-pdf"
    assert search_articles("квантовий телепортатор") == []
    assert 5 <= len(ARTICLES) <= 10
    assert len({a.id for a in ARTICLES}) == len(ARTICLES)


@pytest.mark.parametrize(
    "candidate",
    [
        Candidate(message="hello", article_ids=["invented"]),
        Candidate(message="https://evil.example", article_ids=["export-pdf"]),
        Candidate(message="[click](evil)", article_ids=["export-pdf"]),
        Candidate(message="hello", article_ids=["export-pdf", "export-pdf"]),
    ],
)
def test_rejects_untrusted_citations(candidate: Candidate) -> None:
    with pytest.raises(RetryableError):
        validate_sources(candidate, [ARTICLES[0]])


@pytest.mark.parametrize("outcome", [200, 401, 503, "timeout"])
async def test_no_key_always_calls_provider_then_uses_delayed_static_result(outcome) -> None:
    requests = []
    sleeps = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "authorization" not in request.headers
        assert "PRIVATE_CUSTOMER" not in request.content.decode()
        if outcome == "timeout":
            raise httpx.ReadTimeout("offline", request=request)
        return httpx.Response(outcome, text="ignored provider response")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await OpenAIAdapter(
            Settings(_env_file=None, openai_api_key=""), client, sleep=fake_sleep, delay=lambda: 5
        ).generate("PRIVATE_CUSTOMER", [ARTICLES[0]])
    assert len(requests) == 1
    assert requests[0].url == "https://api.openai.com/v1/responses"
    assert result.article_ids == ["export-pdf"]
    assert ARTICLES[0].body in result.message
    assert 4.5 < sleeps[0] <= 5


def provider_response(
    message: str = "Натисніть Експорт → PDF.", article_id: str = "export-pdf"
) -> dict:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"message": message, "article_ids": [article_id]}),
                    }
                ],
            }
        ],
    }


async def test_keyed_adapter_uses_responses_schema_and_validates_output() -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-only"
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["properties"]["article_ids"]["items"]["enum"] == [
            "export-pdf"
        ]
        return httpx.Response(200, json=provider_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await OpenAIAdapter(
            Settings(_env_file=None, openai_api_key="test-only"), client
        ).generate("PDF", [ARTICLES[0]])
    assert result.article_ids == ["export-pdf"]


@pytest.mark.parametrize(
    "status,error",
    [(429, RetryableError), (503, RetryableError), (401, PermanentError), (400, PermanentError)],
)
async def test_provider_failures_have_no_internal_retries(status, error) -> None:
    calls = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(error):
            await OpenAIAdapter(
                Settings(_env_file=None, openai_api_key="test-only"), client
            ).generate("PDF", [ARTICLES[0]])
    assert calls == 1


@pytest.mark.parametrize(
    "body",
    [
        provider_response(article_id="invented"),
        provider_response(message="https://evil.example"),
        {"status": "incomplete"},
        [],
        {"status": "completed", "output": []},
    ],
)
async def test_invalid_provider_output_is_retryable(body) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(RetryableError):
            await OpenAIAdapter(
                Settings(_env_file=None, openai_api_key="test-only"), client
            ).generate("PDF", [ARTICLES[0]])


async def test_article_links_resolve_to_real_content() -> None:
    app = create_app(Settings(_env_file=None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for article in ARTICLES:
            response = await client.get(article.url)
            assert response.status_code == 200 and article.title in response.text
        assert (await client.get("/kb/missing")).status_code == 404


async def test_missing_sources_never_contacts_provider() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("No sources should prevent a provider call")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(PermanentError, match="no_sources"):
            await OpenAIAdapter(Settings(_env_file=None), client).generate("question", [])


@pytest.mark.parametrize("error", [httpx.ReadTimeout("offline"), TimeoutError()])
async def test_keyed_network_failure_is_retryable(error: Exception) -> None:
    calls = 0

    def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(RetryableError, match="provider_network_error"):
            await OpenAIAdapter(
                Settings(_env_file=None, openai_api_key="test-only"), client
            ).generate("PDF", [ARTICLES[0]])
    assert calls == 1


async def test_no_key_hanging_provider_is_bounded_by_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HTTPX's mock transport doesn't enforce socket timeouts; exercise our outer deadline.
    real_timeout = asyncio.timeout
    monkeypatch.setattr("app.llm.asyncio.timeout", lambda seconds: real_timeout(0.02))
    delays = []

    async def hanging(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(hanging)) as client:
        result = await OpenAIAdapter(
            Settings(_env_file=None, openai_api_key=""), client, sleep=sleep, delay=lambda: 5
        ).generate("question", [ARTICLES[0]])
    assert result.article_ids == ["export-pdf"]
    assert 4 < delays[0] < 5
