"""HTTP boundary tests use only an ephemeral loopback server and fake service."""
from __future__ import annotations

import http.client
import json
import os
import sys
from datetime import datetime, timezone
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from hotel_bot import server as server_module
from hotel_bot.server import MAX_BODY_BYTES, create_server, main


class FakeService:
    def __init__(self):
        self.messages = []
        self.approvals = []
        self.journal_calls = []
        self.failure = None
        self.stopping = False

    def stop_accepting(self):
        self.stopping = True

    def snapshot(self):
        if self.failure:
            raise self.failure
        return {"rooms": [], "provider": {"status": "not_checked"}, "demo": True}

    def submit_message(self, payload):
        if self.failure:
            raise self.failure
        self.messages.append(payload)
        return {"request_id": payload.get("request_id", "test-request"), "replayed": False,
                "request": payload, "snapshot": self.snapshot()}

    def approve(self, proposal_id, decision, consent=False):
        if self.failure:
            raise self.failure
        self.approvals.append((proposal_id, decision, consent))
        return {"proposal_id": proposal_id, "replayed": False,
                "proposal": {"decision": decision, "consent": consent}, "snapshot": self.snapshot()}

    def journal(self, after=0, limit=100):
        if self.failure:
            raise self.failure
        self.journal_calls.append((after, limit))
        return {"entries": [{"id": after + 1, "reason": "Synthetic operator journal"}],
                "next_cursor": after + 1, "total": after + 1}


class FakeReplayManager:
    def __init__(self, dataset_root=None, output_root=None):
        self.dataset_root = dataset_root
        self.output_root = output_root
        self.starts, self.polls, self.cancellations, self.downloads = [], [], [], []
        self.playback_calls = []
        self.import_calls = []
        self.artifacts = {}
        self.failure = None
        self.closed = False

    def _check(self):
        if self.failure:
            raise self.failure

    def list_datasets(self):
        self._check()
        return [{"id": "quickstart", "available": True}, {"id": "dev", "available": True}]

    def start(self, dataset_id):
        self._check()
        if dataset_id not in {"quickstart", "dev"}:
            raise ValueError("Unknown dataset")
        self.starts.append(dataset_id)
        return {"id": "replay-test-1", "dataset_id": dataset_id, "status": "queued"}

    def status(self, job_id, after=0, limit=100):
        self._check()
        self.polls.append((job_id, after, limit))
        return {"job": {"id": job_id, "status": "running"}, "snapshot": {"source": "dataset"},
                "timeline": [{"id": after + 1, "reason": "An observed input requires review"}],
                "next_cursor": after + 1, "total": after + 1, "has_more": False}

    def cancel(self, job_id):
        self._check()
        self.cancellations.append(job_id)
        return {"id": job_id, "status": "running", "cancel_requested": True}

    def artifact(self, job_id, kind):
        self._check()
        self.downloads.append((job_id, kind))
        if kind not in self.artifacts:
            error = ValueError("Artifact not ready")
            error.status_code = 409
            raise error
        return self.artifacts[kind]

    def close(self, **kwargs):
        self.closed = True
        return True

    def playback_manifest(self, job_id):
        self._check()
        self.playback_calls.append(("manifest", job_id))
        return {"job_id": job_id, "total_events": 10, "mode": "archived_read_only"}

    def playback_frame(self, job_id, **selection):
        self._check()
        self.playback_calls.append(("frame", job_id, selection))
        return {"events_consumed": selection.get("cursor", 2), "snapshot": {"rooms": []}}

    def timeline(self, job_id, **page):
        self._check()
        self.playback_calls.append(("timeline", job_id, page))
        return {"events": [{"ordinal": page["after"] + 1}], "next_cursor": page["after"] + 1, "total": 10}

    def import_dataset(self, stream, length, *, cancelled):
        self._check()
        assert not cancelled()
        self.import_calls.append(stream.read(length))
        return {"dataset": {"id": "dataset-" + "a" * 64, "name": "Imported test", "imported": True,
                            "available": True, "replay_id": None}, "duplicate": False}


@pytest.fixture
def live_server(tmp_path):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Hotel test</title>")
    (static / "app.js").write_text("window.hotelTest = true;")
    service = FakeService()
    replays = FakeReplayManager(tmp_path / 'data', tmp_path / 'replays')
    server = create_server(service, port=0, static_dir=static, replay_manager=replays)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    yield server, service, static
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)
    assert not worker.is_alive()


def call(server, method="GET", path="/api/state", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def post(server, path, value, headers=None):
    return call(server, "POST", path, json.dumps(value).encode(),
                {"Content-Type": "application/json", **(headers or {})})


def test_get_state_is_direct_service_snapshot_and_startup_makes_no_model_call(live_server):
    server, service, _ = live_server
    status, headers, body = call(server)
    assert status == 200 and json.loads(body) == service.snapshot()
    assert json.loads(body)["provider"]["status"] == "not_checked"
    assert service.messages == [] and service.approvals == []
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers


def test_message_and_approval_delegate_without_inventing_actions(live_server):
    server, service, _ = live_server
    origin = f"http://127.0.0.1:{server.server_port}"
    value = {"text": "Please help with the key card.", "room_id": "R101", "request_id": "browser-id-1"}
    status, _, body = post(server, "/api/messages", value, {"Origin": origin})
    assert status == 200 and service.messages == [value]
    assert json.loads(body)["request_id"] == "browser-id-1"
    assert json.loads(body)["snapshot"] == service.snapshot()
    status, _, body = post(server, "/api/approvals/proposal-1", {"decision": "approve", "consent": True})
    assert status == 200 and service.approvals == [("proposal-1", "approve", True)]
    assert json.loads(body)["proposal"]["consent"] is True
    status, _, _ = post(server, "/api/approvals/proposal-2", {"decision": "reject"})
    assert status == 200 and service.approvals[-1] == ("proposal-2", "reject", False)


def test_dataset_listing_never_starts_a_replay_and_start_is_explicit(live_server):
    server, _, _ = live_server
    manager = server.replay_manager
    status, _, body = call(server, path='/api/datasets')
    assert status == 200 and json.loads(body) == {'datasets': manager.list_datasets()}
    assert manager.starts == manager.polls == manager.cancellations == []
    status, _, body = post(server, '/api/replays', {'dataset_id': 'quickstart'})
    assert status == 200 and manager.starts == ['quickstart']
    assert json.loads(body)['job'] == {'id': 'replay-test-1', 'dataset_id': 'quickstart', 'status': 'queued'}
    assert str(manager.dataset_root).encode() not in body
    assert str(manager.output_root).encode() not in body


def test_replay_poll_and_full_journal_use_exclusive_cursor_pagination(live_server):
    server, service, _ = live_server
    status, _, body = call(server, path='/api/replays/replay-test-1?after=12&limit=25')
    assert status == 200
    assert server.replay_manager.polls == [('replay-test-1', 12, 25)]
    result = json.loads(body)
    assert result['timeline'][0]['id'] == result['next_cursor'] == 13
    assert result['job']['status'] == 'running' and not result['has_more']
    status, _, body = call(server, path='/api/journal?after=7&limit=3')
    assert status == 200 and service.journal_calls == [(7, 3)]
    assert json.loads(body)['entries'][0]['id'] == 8
    assert call(server, path='/api/journal')[0] == 200
    assert service.journal_calls[-1] == (0, 100)
    assert server.replay_manager.starts == []
    assert call(server, path='/api/replays/replay-test-1?limit=201')[0] == 400
    assert call(server, path='/api/journal?limit=500')[0] == 200


@pytest.mark.parametrize('query', ['after=-1', 'after=abc', 'after=', 'after=1.0',
                                  'after=1&after=2', 'limit=0', 'limit=501',
                                  'limit=2&path=secret', 'after=1&limit=2&extra=1',
                                  'path=../../private', 'limit', 'after=1&', 'after=%ff'])
@pytest.mark.parametrize('route', ['/api/journal', '/api/replays/replay-test-1'])
def test_pagination_rejects_ambiguous_or_unbounded_queries_before_service(live_server, query, route):
    server, service, _ = live_server
    assert call(server, path=route + '?' + query)[0] == 400
    assert service.journal_calls == server.replay_manager.polls == []


@pytest.mark.parametrize('payload', [{}, {'dataset_id': None}, {'dataset_id': []},
                                    {'dataset_id': '../private'}, {'dataset_id': 'holdout'},
                                    {'dataset_id': 'quickstart', 'path': '/tmp/private'}])
def test_replay_start_accepts_only_known_dataset_identity(live_server, payload):
    server, _, _ = live_server
    assert post(server, '/api/replays', payload)[0] == 400
    assert server.replay_manager.starts == []


def test_cancellation_only_signals_owned_job_and_returns_without_polling(live_server):
    server, _, _ = live_server
    status, _, body = post(server, '/api/replays/replay-test-1/cancel', {})
    assert status == 200
    assert json.loads(body)['job']['cancel_requested'] is True
    assert server.replay_manager.cancellations == ['replay-test-1']
    assert server.replay_manager.polls == server.replay_manager.starts == []
    assert post(server, '/api/replays/replay-test-1/cancel', {'wait': True})[0] == 400
    assert server.replay_manager.cancellations == ['replay-test-1']


@pytest.mark.parametrize('route', ['/api/replays', '/api/replays/replay-test-1/cancel'])
def test_foreign_origin_cannot_start_or_cancel_replay(live_server, route):
    server, _, _ = live_server
    assert post(server, route, {'dataset_id': 'quickstart'} if route == '/api/replays' else {},
                {'Origin': 'https://attacker.invalid'})[0] == 403
    assert server.replay_manager.starts == server.replay_manager.cancellations == []


def test_scoped_artifact_download_and_head_do_not_accept_arbitrary_paths(live_server):
    server, _, static = live_server
    file = static.parent / 'replay-output.jsonl'
    content = b'{"action":"request_human_approval"}\n'
    file.write_bytes(content)
    server.replay_manager.artifacts['decisions'] = file
    route = '/api/replays/replay-test-1/artifacts/decisions'
    status, headers, body = call(server, path=route)
    assert status == 200 and body == content
    assert headers['Content-Disposition'] == 'attachment; filename="decisions.jsonl"'
    assert headers['Content-Type'] == 'application/x-ndjson'
    assert server.replay_manager.downloads == [('replay-test-1', 'decisions')]
    status, headers, body = call(server, 'HEAD', route)
    assert status == 200 and body == b'' and int(headers['Content-Length']) == len(content)
    for suffix in ('../../private.key', '%2e%2e/private.key', 'private.key'):
        status, _, _ = call(server, path='/api/replays/replay-test-1/artifacts/' + suffix)
        assert status == 404
    assert call(server, path=route + '?path=/tmp/private.key')[0] == 400
    assert server.replay_manager.downloads == [('replay-test-1', 'decisions')] * 2
    assert call(server, path='/api/replays/replay-test-1/artifacts/summary')[0] == 409


@pytest.mark.parametrize('status_code', [400, 404, 409])
def test_replay_errors_preserve_status_without_exposing_filesystem_or_provider_text(live_server, status_code):
    server, _, _ = live_server
    error = ValueError('/private/secret-folder synthetic-provider-credential')
    error.status_code = status_code
    server.replay_manager.failure = error
    for route in ('/api/datasets', '/api/replays/replay-test-1',
                  '/api/replays/replay-test-1/artifacts/summary'):
        status, _, body = call(server, path=route)
        assert status == status_code
        assert b'secret-folder' not in body and b'synthetic-provider-credential' not in body


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.invalid"},
    {"Host": "localhost.attacker.invalid:8765"},
    {"Host": "127.0.0.1:1"},
    {"Origin": "https://attacker.invalid"},
    {"Origin": "null"},
    {"Origin": "http://user:secret@127.0.0.1:8765"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_foreign_origin_dns_rebinding_or_cross_site_posts_cannot_mutate_state(live_server, headers):
    server, service, _ = live_server
    status, response_headers, _ = post(server, "/api/messages", {"text": "x", "room_id": "R101"}, headers)
    assert status == 403
    assert service.messages == [] and "Access-Control-Allow-Origin" not in response_headers


def test_origin_must_match_actual_host_and_port_and_preflight_is_not_enabled(live_server):
    server, service, _ = live_server
    value = {"text": "x", "room_id": "R101"}
    status, _, _ = post(server, "/api/messages", value, {"Origin": f"http://localhost:{server.server_port}"})
    assert status == 403 and service.messages == []
    status, headers, _ = call(server, "OPTIONS", "/api/messages", headers={
        "Origin": "https://attacker.invalid", "Access-Control-Request-Method": "POST"})
    assert status == 405 and "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"])
def test_form_and_non_json_posts_are_rejected_before_service(live_server, content_type):
    server, service, _ = live_server
    status, _, _ = call(server, "POST", "/api/messages", b'{"text":"x"}', {"Content-Type": content_type})
    assert status == 415 and service.messages == []


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b'{}{}', b'{"text":NaN}', b'{"text":"a","text":"b"}', b'"\xff"'])
def test_malformed_nonobject_duplicate_and_nonfinite_json_fail_closed(live_server, body):
    server, service, _ = live_server
    status, _, _ = call(server, "POST", "/api/messages", body, {"Content-Type": "application/json"})
    assert status == 400 and service.messages == []


def test_oversized_json_is_rejected_without_service_call(live_server):
    server, service, _ = live_server
    status, _, _ = call(server, "POST", "/api/messages", b"x" * (MAX_BODY_BYTES + 1),
                        {"Content-Type": "application/json"})
    assert status == 413 and service.messages == []


@pytest.mark.parametrize("extra_headers,expected", [
    ([], 411),
    ([("Content-Length", "-1")], 400),
    ([("Content-Length", "2"), ("Content-Length", "2")], 400),
    ([("Transfer-Encoding", "chunked")], 400),
])
def test_missing_ambiguous_or_chunked_body_lengths_are_rejected(live_server, extra_headers, expected):
    server, service, _ = live_server
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        conn.putrequest("POST", "/api/messages")
        conn.putheader("Content-Type", "application/json")
        for key, value in extra_headers:
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == expected
        response.read()
    finally:
        conn.close()
    assert service.messages == []


@pytest.mark.parametrize("payload", [
    {}, {"decision": "release_room"}, {"decision": "approve", "consent": "yes"},
    {"decision": "approve", "consent": 1}, {"decision": "approve", "extra": "field"},
    {"decision": []}, {"decision": {}},
])
def test_approval_boundary_requires_exact_decision_and_boolean_consent(live_server, payload):
    server, service, _ = live_server
    status, _, _ = post(server, "/api/approvals/one", payload)
    assert status == 400 and service.approvals == []


@pytest.mark.parametrize("status_code", [400, 404, 409, 999])
def test_domain_errors_have_bounded_statuses_and_never_expose_exception_text(live_server, status_code, capsys):
    server, service, _ = live_server
    class DomainError(ValueError):
        pass
    failure = DomainError("synthetic-provider-secret private-guest-data")
    failure.status_code = status_code
    service.failure = failure
    status, _, body = post(server, "/api/messages", {"text": "x"})
    assert status == (status_code if status_code in {400, 404, 409} else 400)
    assert b"synthetic-provider-secret" not in body and b"private-guest-data" not in body
    captured = capsys.readouterr()
    assert "synthetic-provider-secret" not in captured.out + captured.err


def test_unexpected_errors_do_not_leak_credentials_or_tracebacks(live_server, capsys):
    server, service, _ = live_server
    service.failure = RuntimeError("synthetic-api-key-this-must-never-appear")
    for method in ("GET", "POST"):
        if method == "GET":
            status, _, body = call(server)
        else:
            status, _, body = post(server, "/api/messages", {"text": "x"})
        assert status == 500 and b"synthetic-api-key" not in body
        assert json.loads(body)["error"]["code"] == "internal_error"
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "synthetic-api-key" not in captured.out + captured.err


def test_static_files_and_head_work_without_directory_listing(live_server):
    server, _, static = live_server
    status, headers, body = call(server, path="/")
    assert status == 200 and b"Hotel test" in body
    assert headers["Content-Type"].startswith("text/html")
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    status, headers, body = call(server, path="/app.js")
    assert status == 200 and b"hotelTest" in body
    assert "javascript" in headers["Content-Type"]
    status, headers, body = call(server, "HEAD", "/")
    assert status == 200 and body == b"" and int(headers["Content-Length"]) > 0
    (static / "assets").mkdir()
    assert call(server, path="/assets/")[0] == 404
    assert call(server, path="/api/unknown")[0] == 404


@pytest.mark.parametrize("path", ["/../private.key", "/%2e%2e/private.key", "/escape.key", "/%00", "/%5c..%5cprivate.key"])
def test_static_paths_never_escape_root_or_read_symlinked_private_files(live_server, path):
    server, _, static = live_server
    outside = static.parent / "private.key"
    outside.write_text("synthetic-private-file-contents")
    (static / "escape.key").symlink_to(outside)
    status, _, body = call(server, path=path)
    assert status in {400, 404} and b"synthetic-private-file-contents" not in body


def test_missing_frontend_has_clear_503_but_state_api_remains_usable(live_server):
    server, _, static = live_server
    (static / "index.html").unlink()
    assert call(server, path="/")[0] == 503
    assert call(server)[0] == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.1", "attacker.invalid", "::"])
def test_factory_does_not_expose_the_demo_on_public_interfaces(host, tmp_path):
    with pytest.raises(ValueError):
        create_server(FakeService(), host=host, port=0, static_dir=tmp_path)


def test_cli_missing_key_exits_without_network_or_echoing_input_path(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    result = main(["--key-file", str(tmp_path / "secret-owner-key")])
    assert result == 2
    captured = capsys.readouterr()
    assert "CEREBRAS_API_KEY" in captured.err
    assert "secret-owner-key" not in captured.err


def test_cli_installs_profile_once_restores_environment_and_closes_server(monkeypatch, tmp_path, capsys):
    key = "csk-synthetic-startup-test-key-for-hotel"
    monkeypatch.setenv("CEREBRAS_API_KEY", key)
    monkeypatch.setenv("LLM_MODEL", "previous-local-model")
    before = dict(os.environ)
    service = object()
    monkeypatch.setitem(sys.modules, "hotel_bot.service", SimpleNamespace(HotelService=lambda: service))
    replay_manager = FakeReplayManager()
    replay_close_calls = []

    def finish_replay(**kwargs):
        replay_close_calls.append(kwargs)
        assert os.environ["LLM_MODEL"] == "gpt-oss-120b"
        replay_manager.closed = len(replay_close_calls) == 2
        return replay_manager.closed

    replay_manager.close = finish_replay
    def manager_factory(**kwargs):
        replay_manager.dataset_root = kwargs['dataset_root']
        replay_manager.output_root = kwargs['output_root']
        return replay_manager
    monkeypatch.setitem(sys.modules, "hotel_bot.replay", SimpleNamespace(ReplayManager=manager_factory))
    captured = {}

    class StubServer:
        server_port = 8765

        def serve_forever(self, **kwargs):
            from ingestion.guest_parser import _config
            actual = _config()
            assert actual["api_key"] == key and actual["model"] == "gpt-oss-120b"
            assert actual["response_format_mode"] == "json_schema"
            captured["served"] = True
            raise KeyboardInterrupt

        def server_close(self):
            captured["closed"] = True

    def create(actual_service, **kwargs):
        assert actual_service is service
        captured["args"] = kwargs
        return StubServer()

    monkeypatch.setattr(server_module, "create_server", create)
    assert main(["--static-dir", str(tmp_path), '--dataset-root', str(tmp_path/'data'),
                 '--replay-output-root', str(tmp_path/'replays')]) == 0
    assert captured["served"] and captured["closed"]
    assert captured["args"]["host"] == "127.0.0.1"
    assert captured['args']['replay_manager'] is replay_manager
    assert replay_manager.closed
    assert replay_close_calls == [{"wait": True, "timeout": 65}] * 2
    assert replay_manager.dataset_root == tmp_path/'data'
    assert replay_manager.output_root == tmp_path/'replays'
    assert dict(os.environ) == before
    output = capsys.readouterr()
    assert key not in output.out + output.err
    assert "http://127.0.0.1:8765" in output.out


def test_http_real_service_flow_preserves_idempotency_and_required_guest_consent(live_server):
    from common.contracts.schemas import ExtractionMetaV1, GuestIntentV1
    from hotel_bot.service import HotelService

    server, _, _ = live_server
    calls = []

    def parse(record, context):
        calls.append((record, context))
        return [GuestIntentV1(intent="water_issue", evidence_quote=record["text"]).model_dump()], ExtractionMetaV1(
            method="llm", model_name="gpt-oss-120b", needs_review=False, uncertain_fields=[])

    server.service = HotelService(parser=parse, clock=lambda: datetime(2026, 11, 16, 9, tzinfo=timezone.utc))
    value = {"request_id": "http-integration-1", "text": "The tap keeps dripping.", "room_id": "R208", "language": "en"}
    status, _, body = post(server, "/api/messages", value)
    assert status == 200
    state = json.loads(body)["snapshot"]
    assert state["model"]["status"] == "result_available"
    assert state["property"]["mode"] == "synthetic_demo"
    ticket = state["proposals"][0]
    assert ticket["requires_consent"] and ticket["status"] == "pending_approval"
    status, _, body = post(server, "/api/messages", value)
    assert status == 200 and json.loads(body)["replayed"] and len(calls) == 1
    path = "/api/approvals/" + ticket["id"]
    status, _, body = post(server, path, {"decision": "approve", "consent": False})
    assert status == 200
    proposal = json.loads(body)["proposal"]
    assert proposal["status"] == "pending_guest_consent" and proposal["effects_applied"] is False
    status, _, body = post(server, path, {"decision": "approve", "consent": True})
    assert status == 200
    proposal = json.loads(body)["proposal"]
    assert proposal["status"] == "approved_simulated" and proposal["effects_applied"] is False
    assert proposal["audit"]["observed_history_changed"] is False
    status, _, body = post(server, path, {"decision": "reject"})
    assert status == 409 and json.loads(body)["error"]["code"] == "conflict"


def test_stop_accepting_rejects_all_new_actions_but_keeps_state_readable(live_server):
    server, service, _ = live_server
    server.stop_accepting()
    server.stop_accepting()
    for path, value in [
        ("/api/messages", {"text": "Please help", "room_id": "R208"}),
        ("/api/approvals/proposal-1", {"decision": "approve", "consent": True}),
        ("/api/replays", {"dataset_id": "quickstart"}),
        ("/api/replays/replay-test-1/cancel", {}),
    ]:
        status, _, body = post(server, path, value)
        assert status == 503 and json.loads(body)["error"]["code"] == "service_unavailable"
    assert service.stopping and server.replay_manager.closed
    assert service.messages == service.approvals == server.replay_manager.starts == []
    assert call(server)[0] == 200


def test_archived_playback_routes_only_read_manifest_source_pages_and_causal_frames(live_server):
    server, service, _ = live_server
    manager = server.replay_manager
    base = "/api/replays/replay-test-1"
    status, _, body = call(server, path=base + "/playback")
    assert status == 200 and json.loads(body)["mode"] == "archived_read_only"
    assert call(server, path=base + "/frame")[0] == 200
    assert call(server, path=base + "/frame?cursor=0")[0] == 200
    assert call(server, path=base + "/frame?at=2026-01-06T09%3A00%3A00Z")[0] == 200
    assert call(server, path=base + "/timeline?after=3&limit=2000")[0] == 200
    assert manager.playback_calls == [
        ("manifest", "replay-test-1"),
        ("frame", "replay-test-1", {"cursor": 0}),
        ("frame", "replay-test-1", {"cursor": 0}),
        ("frame", "replay-test-1", {"at": "2026-01-06T09:00:00Z"}),
        ("timeline", "replay-test-1", {"after": 3, "limit": 2000}),
    ]
    assert manager.starts == service.messages == service.approvals == []


@pytest.mark.parametrize("suffix", ["playback?cursor=0", "frame?cursor=-1", "frame?cursor=true",
    "frame?cursor=1&cursor=2", "frame?cursor=1&at=2026-01-01T00:00:00Z", "frame?at=tomorrow",
    "frame?at=2026-01-01T00:00:00", "frame?unknown=1", "timeline?limit=2001", "timeline?after=-1"])
def test_playback_rejects_ambiguous_or_unbounded_seeks_before_manager(live_server, suffix):
    server, _, _ = live_server
    assert call(server, path="/api/replays/replay-test-1/" + suffix)[0] == 400
    assert server.replay_manager.playback_calls == []


def test_unprepared_archive_is_explicit_conflict_not_new_model_work(live_server):
    from hotel_bot.replay import ReplayConflictError
    server, service, _ = live_server
    server.replay_manager.failure = ReplayConflictError("Synthetic incomplete archive")
    status, _, body = call(server, path="/api/replays/replay-test-1/playback")
    assert status == 409 and json.loads(body)["error"]["code"] == "conflict"
    assert server.replay_manager.starts == service.messages == []


def test_binary_zip_upload_is_separate_from_json_size_limit_and_never_starts_model(live_server):
    server, service, _ = live_server
    data = b"PK synthetic HTTP-only ZIP body " * 2000
    status, _, body = call(server, "POST", "/api/datasets/import", data, {"Content-Type": "application/zip"})
    assert status == 200 and json.loads(body)["dataset"]["imported"]
    assert not json.loads(body)["duplicate"]
    assert len(data) > MAX_BODY_BYTES and server.replay_manager.import_calls == [data]
    assert server.replay_manager.starts == service.messages == service.approvals == []


@pytest.mark.parametrize("headers,status", [
    ({"Content-Type": "application/json"}, 415),
    ({"Content-Type": "multipart/form-data"}, 415),
    ({"Content-Type": "application/zip", "Content-Length": "0"}, 413),
    ({"Content-Type": "application/zip", "Content-Length": str(128 * 1024 * 1024 + 1)}, 413),
    ({"Content-Type": "application/zip", "Content-Length": "12.0"}, 400),
    ({"Content-Type": "application/zip", "Transfer-Encoding": "chunked"}, 400),
    ({"Content-Type": "application/zip", "Origin": "https://external.example"}, 403),
])
def test_invalid_zip_upload_headers_are_rejected_before_reading_or_import(live_server, headers, status):
    server, _, _ = live_server
    result, _, _ = call(server, "POST", "/api/datasets/import", b"", headers)
    assert result == status and server.replay_manager.import_calls == []


def test_zip_validation_error_is_actionable_without_leaking_internal_paths(live_server):
    from hotel_bot.datasets import DatasetImportError
    server, _, _ = live_server
    server.replay_manager.failure = DatasetImportError("encrypted_zip", "Decrypt the ZIP locally and upload an unencrypted ZIP.")
    status, _, body = call(server, "POST", "/api/datasets/import", b"PK", {"Content-Type": "application/zip"})
    assert status == 400
    assert json.loads(body) == {"error": {"code": "encrypted_zip", "message": "Decrypt the ZIP locally and upload an unencrypted ZIP."}}
    assert server.replay_manager.starts == []


def test_shutdown_drains_accepted_body_without_starting_an_action(live_server, monkeypatch):
    server, service, _ = live_server
    entered = Event()
    body = server_module.HotelRequestHandler._body

    def reading_body(handler):
        entered.set()
        return body(handler)

    monkeypatch.setattr(server_module.HotelRequestHandler, "_body", reading_body)
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    payload = json.dumps({"text": "Help with access", "room_id": "R208"}).encode()
    closed = Event()

    def close():
        server.server_close()
        closed.set()

    closer = Thread(target=close, daemon=True)
    try:
        connection.putrequest("POST", "/api/messages")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", len(payload))
        connection.endheaders()
        assert entered.wait(2)
        server.shutdown()
        closer.start()
        assert not closed.wait(0.05)
        connection.send(payload)
        response = connection.getresponse()
        assert response.status == 503
        response.read()
        assert closed.wait(2)
        assert service.messages == []
    finally:
        connection.close()
        if closer.ident is not None:
            closer.join(2)


def test_cli_shutdown_keeps_provider_until_active_handler_finishes_and_rejects_queue(monkeypatch, tmp_path):
    from common.contracts.schemas import ExtractionMetaV1, GuestIntentV1
    from hotel_bot.journal import SessionJournal
    from hotel_bot.service import HotelService, ServiceUnavailableError

    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-synthetic-lifecycle-test-key")
    monkeypatch.setenv("LLM_MODEL", "previous-local-model")
    previous_environment = dict(os.environ)
    parsing, release, queued, ready, completed = (Event() for _ in range(5))
    parser_calls, parser_models = [], []

    def parser(record, context):
        parser_calls.append(record["request_id"])
        parser_models.append(os.environ.get("LLM_MODEL"))
        parsing.set()
        assert release.wait(5)
        parser_models.append(os.environ.get("LLM_MODEL"))
        return [GuestIntentV1(intent="wifi_help", evidence_quote=record["text"]).model_dump()], ExtractionMetaV1(
            method="llm", model_name="gpt-oss-120b", needs_review=False, uncertain_fields=[])

    service = HotelService(parser=parser, clock=lambda: datetime(2026, 11, 16, 9, tzinfo=timezone.utc),
                           journal=SessionJournal(root=tmp_path / "journal"))
    submit = service.submit_message

    def submit_with_arrival(payload):
        if payload["request_id"] == "queued-request":
            queued.set()
        return submit(payload)

    service.submit_message = submit_with_arrival
    monkeypatch.setitem(sys.modules, "hotel_bot.service", SimpleNamespace(HotelService=lambda: service))
    manager = FakeReplayManager()
    monkeypatch.setitem(sys.modules, "hotel_bot.replay", SimpleNamespace(ReplayManager=lambda **kwargs: manager))
    factory, captured, outcomes = server_module.create_server, {}, {}

    def create(*args, **kwargs):
        captured["server"] = factory(*args, **kwargs)
        ready.set()
        return captured["server"]

    monkeypatch.setattr(server_module, "create_server", create)

    def run():
        try:
            outcomes["exit"] = main(["--port", "0", "--static-dir", str(tmp_path)])
        finally:
            completed.set()

    runner = Thread(target=run, daemon=True)
    clients = []
    runner.start()
    try:
        assert ready.wait(2)
        server = captured["server"]

        def message(name):
            outcomes[name] = post(server, "/api/messages", {
                "request_id": name, "room_id": "R208", "text": "Please help with Wi-Fi.", "language": "en"})

        clients.append(Thread(target=message, args=("active-request",), daemon=True))
        clients[-1].start()
        assert parsing.wait(2)
        clients.append(Thread(target=message, args=("queued-request",), daemon=True))
        clients[-1].start()
        assert queued.wait(2)
        server.shutdown()
        assert service._stopping.is_set()
        assert not completed.wait(0.05)
        assert os.environ["LLM_MODEL"] == "gpt-oss-120b"
        release.set()
        for client in clients:
            client.join(2)
            assert not client.is_alive()
        assert completed.wait(2)
        assert outcomes["exit"] == 0
        assert outcomes["active-request"][0] == 200
        assert outcomes["queued-request"][0] == 503
        assert parser_calls == ["active-request"]
        assert parser_models == ["gpt-oss-120b", "gpt-oss-120b"]
        assert manager.closed
        assert dict(os.environ) == previous_environment
        with pytest.raises(ServiceUnavailableError):
            service.approve("unused-proposal", "approve")
        with pytest.raises(ServiceUnavailableError):
            service.submit_message({"request_id": "after-close"})
    finally:
        release.set()
        if "server" in captured:
            captured["server"].shutdown()
        for client in clients:
            client.join(2)
        runner.join(2)
