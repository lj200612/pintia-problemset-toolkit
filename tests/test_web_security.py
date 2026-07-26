from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pta.webapp as webapp
import pta_web
from pta.config import Config


class _ExamClient:
    exam_status = "PROCESSING"
    exam_ended = False
    compilers = ["GCC"]
    submission = {"submissionId": "submission-1"}
    submit_calls = 0

    def __init__(self, _cfg):
        pass

    def get_exam(self):
        return {
            "exam": {"id": "exam-1", "status": self.exam_status, "ended": self.exam_ended},
            "problemSet": {"problemSetConfig": {"compilers": self.compilers}},
        }

    def get_problem_status(self, _exam_id):
        return {"problemStatus": []}

    def get_problem_types(self):
        return {
            "labels": [
                {
                    "id": "123",
                    "title": "Synthetic problem",
                    "type": "PROGRAMMING",
                    "indexInProblemPool": 0,
                }
            ]
        }

    def submit(self, _exam_id, _payload):
        type(self).submit_calls += 1
        return dict(self.submission)


class _OverviewClient(_ExamClient):
    def get_exam_problem_status(self):
        return {"problemStatus": []}


class _QueuedProblemSetClient:
    calls = 0

    def __init__(self, _cfg):
        pass

    def get_my_problem_sets(self, *, only_active):
        type(self).calls += 1
        return {
            "problemSets": [
                {
                    "id": f"response-{type(self).calls}",
                    "name": "active" if only_active else "all",
                }
            ]
        }


class _ConfigAwareProblemSetClient:
    calls = 0

    def __init__(self, cfg):
        self.cfg = cfg

    def get_my_problem_sets(self, *, only_active):
        type(self).calls += 1
        return {
            "problemSets": [
                {
                    "id": self.cfg.base_url,
                    "name": "active" if only_active else "all",
                }
            ]
        }


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_app(self, name="config.json", **overrides):
        config = {
            "baseUrl": "https://example.invalid",
            "problemSetId": "set-1",
            "cookie": "",
            "answersDir": str(self.root / "answers"),
            "logDir": str(self.root / "logs"),
            "defaultCompiler": "GCC",
        }
        config.update(overrides)
        path = self.root / name
        path.write_text(json.dumps(config), encoding="utf-8")
        app = webapp.create_app(str(path))
        app.config.update(TESTING=True)
        return app

    @staticmethod
    def auth(app):
        return {"Authorization": f"Bearer {app.config['PTA_API_TOKEN']}"}

    def test_api_requires_fresh_token_and_index_bootstraps_it(self):
        app1 = self.make_app("one.json")
        app2 = self.make_app("two.json")
        self.assertNotEqual(app1.config["PTA_API_TOKEN"], app2.config["PTA_API_TOKEN"])

        client = app1.test_client()
        self.assertEqual(client.get("/api/status").status_code, 401)
        self.assertEqual(
            client.get(f"/api/status?_token={app1.config['PTA_API_TOKEN']}").status_code,
            401,
        )
        self.assertEqual(client.get("/api/status", headers=self.auth(app1)).status_code, 200)

        index = client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn(app1.config["PTA_API_TOKEN"], index.get_data(as_text=True))
        self.assertIn("no-store", index.headers.get("Cache-Control", ""))
        self.assertEqual(index.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("script-src 'self'", index.headers.get("Content-Security-Policy", ""))

    def test_werkzeug_token_filter_is_singleton_and_stateless(self):
        app1 = self.make_app("filter-one.json")
        app2 = self.make_app("filter-two.json")
        logger = logging.getLogger("werkzeug")
        token_filters = [
            item for item in logger.filters if isinstance(item, webapp._TokenRedactionFilter)
        ]
        self.assertEqual(token_filters, [webapp._TOKEN_REDACTION_FILTER])

        for query_key, token in (
            ("_token", app1.config["PTA_API_TOKEN"]),
            ("%5Ftoken", app2.config["PTA_API_TOKEN"]),
        ):
            record = logging.LogRecord(
                "werkzeug",
                logging.INFO,
                __file__,
                1,
                "%s",
                (f"GET /api/overview-stream?{query_key}={token}&psid=1 HTTP/1.1",),
                None,
            )
            self.assertTrue(webapp._TOKEN_REDACTION_FILTER.filter(record))
            rendered = record.getMessage()
            self.assertNotIn(token, rendered)
            self.assertIn("[REDACTED]", rendered)

        self.assertNotEqual(app1.config["PTA_API_TOKEN"], app2.config["PTA_API_TOKEN"])

    def test_host_and_origin_must_be_local_and_same_origin(self):
        app = self.make_app()
        client = app.test_client()
        headers = self.auth(app)

        self.assertEqual(
            client.get("/api/status", headers={**headers, "Host": "attacker.example"}).status_code,
            403,
        )
        self.assertEqual(
            client.get(
                "/api/status", headers={**headers, "Origin": "https://attacker.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            client.get(
                "/api/status", headers={**headers, "Origin": "http://localhost"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.get(
                "/api/status", headers={**headers, "Origin": "http://localhost/"}
            ).status_code,
            200,
        )

        for malformed_host in ("[::1]evil", "localhost:not-a-port", "user@localhost"):
            response = client.get("/api/status", headers={**headers, "Host": malformed_host})
            self.assertEqual(response.status_code, 403, malformed_host)

        for malformed_origin in ("http://localhost/path", "http://localhost?query=1"):
            response = client.get("/api/status", headers={**headers, "Origin": malformed_origin})
            self.assertEqual(response.status_code, 403, malformed_origin)

    def test_stream_token_is_endpoint_scoped_and_stream_is_not_cached(self):
        app = self.make_app()
        client = app.test_client()
        token = app.config["PTA_API_TOKEN"]

        with patch.object(webapp, "PTAClient", _OverviewClient):
            response = client.get(f"/api/overview-stream?psid=set-1&_token={token}", buffered=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers.get("Content-Type", ""))
        self.assertIn('"phase": "done"', response.get_data(as_text=True))
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")

        wrong_endpoint = client.get(f"/api/status?_token={token}")
        self.assertEqual(wrong_endpoint.status_code, 401)
        self.assertIn("no-store", wrong_endpoint.headers.get("Cache-Control", ""))

    def test_save_answer_rejects_traversal_and_non_language_extensions(self):
        answers = self.root / "safe-answers"
        app = self.make_app(answersDir=str(answers))
        client = app.test_client()
        headers = self.auth(app)

        for payload in (
            {"pspid": "../escape", "ext": ".c", "code": "x"},
            {"pspid": "C:\\escape", "ext": ".c", "code": "x"},
            {"pspid": "123", "ext": "../../escape", "code": "x"},
            {"pspid": "123", "ext": ".exe", "code": "x"},
        ):
            response = client.post("/api/save-answer", headers=headers, json=payload)
            self.assertEqual(response.status_code, 400, payload)

        response = client.post(
            "/api/save-answer",
            headers=headers,
            json={"pspid": "123", "ext": ".C", "code": "int main(void){}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["path"], "123.c")
        self.assertEqual((answers / "123.c").read_text(encoding="utf-8"), "int main(void){}")
        self.assertFalse((self.root / "escape.c").exists())

    def test_answer_scan_cannot_escape_answers_directory(self):
        answers = self.root / "answers-with-escaped-entry"
        answers.mkdir()
        outside = self.root / "outside.c"
        secret = "SECRET-SOURCE-OUTSIDE-ANSWERS"
        outside.write_text(secret, encoding="utf-8")
        escaped_answer = Mock(path=str(outside), ext=".c")

        app = self.make_app(answersDir=str(answers))
        client = app.test_client()
        headers = self.auth(app)

        with patch.object(webapp, "scan_answers", return_value={"123": escaped_answer}):
            answer = client.get("/api/answer?pspid=123", headers=headers)
            _ExamClient.submit_calls = 0
            with patch.object(webapp, "PTAClient", _ExamClient):
                batch = client.post("/api/batch-submit", headers=headers, json={"psid": "set-1"})

        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.get_json(), {"exists": False})
        self.assertEqual(batch.status_code, 200)
        self.assertEqual(batch.get_json()["results"], [])
        self.assertEqual(_ExamClient.submit_calls, 0)
        escaped_answer.read.assert_not_called()

        # A normal destination remains writable after the escaped entry was ignored.
        save = client.post(
            "/api/save-answer",
            headers=headers,
            json={"pspid": "123", "ext": ".c", "code": "replacement"},
        )
        self.assertEqual(save.status_code, 200)
        self.assertEqual((answers / "123.c").read_text(encoding="utf-8"), "replacement")
        self.assertEqual(outside.read_text(encoding="utf-8"), secret)

    def test_wrong_json_content_type_is_not_converted_to_500(self):
        app = self.make_app()
        response = app.test_client().post(
            "/api/login/manual",
            headers={**self.auth(app), "Content-Type": "text/plain"},
            data="not-json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.is_json)

    def test_all_submit_routes_reject_non_processing_exam(self):
        class NotProcessingClient(_ExamClient):
            exam_status = "READY"
            submit_calls = 0

        app = self.make_app()
        client = app.test_client()
        headers = self.auth(app)
        single_payload = {
            "psid": "set-1",
            "pspid": "123",
            "type": "PROGRAMMING",
            "compiler": "GCC",
            "code": "int main(void){}",
        }
        with patch.object(webapp, "PTAClient", NotProcessingClient):
            self.assertEqual(
                client.post("/api/submit", headers=headers, json=single_payload).status_code, 400
            )
            self.assertEqual(
                client.post(
                    "/api/batch-submit", headers=headers, json={"psid": "set-1"}
                ).status_code,
                400,
            )
            streamed = client.post(
                "/api/batch-submit-stream", headers=headers, json={"psid": "set-1"}
            )
        self.assertEqual(streamed.status_code, 200)
        self.assertIn('"fatal": true', streamed.get_data(as_text=True))
        self.assertIn("no-store", streamed.headers.get("Cache-Control", ""))
        self.assertEqual(NotProcessingClient.submit_calls, 0)

    def test_submit_audit_excludes_source_cookie_and_api_token(self):
        secret_source = "SOURCE-MUST-NOT-BE-LOGGED"
        secret_cookie = "COOKIE-MUST-NOT-BE-LOGGED"
        app = self.make_app(cookie=secret_cookie)
        client = app.test_client()
        _ExamClient.submit_calls = 0
        with patch.object(webapp, "PTAClient", _ExamClient):
            response = client.post(
                "/api/submit",
                headers=self.auth(app),
                json={
                    "psid": "set-1",
                    "pspid": "123",
                    "type": "PROGRAMMING",
                    "compiler": "GCC",
                    "code": secret_source,
                },
            )
        self.assertEqual(response.status_code, 200)
        log_text = next((self.root / "logs").glob("submit-web-*.jsonl")).read_text(encoding="utf-8")
        self.assertIn("submission-1", log_text)
        self.assertNotIn(secret_source, log_text)
        self.assertNotIn(secret_cookie, log_text)
        self.assertNotIn(app.config["PTA_API_TOKEN"], log_text)

    def test_app_factories_keep_independent_config_paths(self):
        app1 = self.make_app("one.json", problemSetId="one")
        app2 = self.make_app("two.json", problemSetId="two")
        first = app1.test_client().get("/api/status", headers=self.auth(app1)).get_json()
        second = app2.test_client().get("/api/status", headers=self.auth(app2)).get_json()
        self.assertEqual(first["problemSetId"], "one")
        self.assertEqual(second["problemSetId"], "two")

    def test_app_factories_do_not_share_executor_or_problem_set_cache(self):
        app1 = self.make_app("shared.json", cookie="same-cookie")
        app2 = webapp.create_app(str(self.root / "shared.json"))
        app2.config.update(TESTING=True)

        self.assertIsNot(
            app1.extensions["pta_web"]["fetch_executor"],
            app2.extensions["pta_web"]["fetch_executor"],
        )
        self.assertIsNot(
            app1.extensions["pta_web"]["problem_sets_cache"],
            app2.extensions["pta_web"]["problem_sets_cache"],
        )

        _QueuedProblemSetClient.calls = 0
        with patch.object(webapp, "PTAClient", _QueuedProblemSetClient):
            first = app1.test_client().get(
                "/api/problem-sets?scope=active", headers=self.auth(app1)
            )
            second = app2.test_client().get(
                "/api/problem-sets?scope=active", headers=self.auth(app2)
            )
        self.assertEqual(first.get_json()["problemSets"][0]["id"], "response-1")
        self.assertEqual(second.get_json()["problemSets"][0]["id"], "response-2")
        self.assertEqual(_QueuedProblemSetClient.calls, 2)

    def test_problem_set_cache_key_tracks_base_url_and_rejects_unknown_scope(self):
        path = self.root / "cache-config.json"
        app = self.make_app(path.name, baseUrl="https://one.invalid", cookie="same-cookie")
        client = app.test_client()
        headers = self.auth(app)

        _ConfigAwareProblemSetClient.calls = 0
        with patch.object(webapp, "PTAClient", _ConfigAwareProblemSetClient):
            first = client.get("/api/problem-sets?scope=active", headers=headers)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["baseUrl"] = "https://two.invalid"
            path.write_text(json.dumps(raw), encoding="utf-8")
            second = client.get("/api/problem-sets?scope=active", headers=headers)
            invalid = client.get("/api/problem-sets?scope=unbounded-key", headers=headers)

        self.assertEqual(first.get_json()["problemSets"][0]["id"], "https://one.invalid")
        self.assertEqual(second.get_json()["problemSets"][0]["id"], "https://two.invalid")
        self.assertEqual(_ConfigAwareProblemSetClient.calls, 2)
        self.assertEqual(invalid.status_code, 400)

    def test_problem_set_cache_identity_preserves_lone_surrogates(self):
        app = self.make_app()
        first = Config(
            "https://example.invalid",
            "set-1",
            "same-cookie",
            extra_headers={"Authorization": "Bearer \ud800"},
        )
        second = Config(
            "https://example.invalid",
            "set-1",
            "same-cookie",
            extra_headers={"Authorization": "Bearer \ud801"},
        )

        with app.app_context():
            first_key = webapp._problem_sets_cache_key(first, "active")
            second_key = webapp._problem_sets_cache_key(second, "active")

        self.assertNotEqual(first_key, second_key)
        self.assertEqual(len(first_key[2]), 64)
        self.assertEqual(len(second_key[2]), 64)

    def test_cli_accepts_only_loopback_hosts(self):
        for host in ("localhost", "127.0.0.1", "127.12.34.56", "::1", "[::1]"):
            self.assertTrue(pta_web.is_loopback_host(host), host)
        for host in ("0.0.0.0", "192.168.1.10", "example.com", ""):
            self.assertFalse(pta_web.is_loopback_host(host), host)


if __name__ == "__main__":
    unittest.main()
