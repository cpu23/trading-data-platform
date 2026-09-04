import errno
import json
import os
import tempfile
import threading
import unittest
import unittest.mock as mock
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pydantic

# unittest discovery imports test modules as top-level modules, so the
# environment must be configured before importing the application modules.
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="trading-api-setup-state-tests-")
os.environ["STATE_DIR"] = _TEST_STATE_DIR
os.environ["DEPLOYMENT_MODE"] = "test"
os.environ.update(
    CSRF_SIGNING_KEY="test-csrf-signing-key-0123456789abcdef",
    SESSION_SIGNING_KEY="test-session-signing-key-0123456789abcdef",
)

import auth  # noqa: E402
import setup_state  # noqa: E402
from routes.json import settings as settings_route  # noqa: E402
from routes.json import setup  # noqa: E402

import config as config_module  # noqa: E402

VALID_PASSWORD = "a sufficiently long password"
VALID_SETUP_TOKEN = "9f2c1a77e4b5d6098a3f1c2e4d5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"
# Environment needed by the real config.validate_candidate (config.yaml refs).
REAL_CONFIG_ENV = {
    "CONFIG_DIR": str(Path(__file__).resolve().parents[2] / "config"),
    "DB_USER": "trading",
    "DB_PASSWORD": "x",
    "DB_NAME": "trading_data",
    "FRED_API_KEY": "fred",
    "OPENROUTER_API_KEY": "sk",
    "OPENROUTER_MODEL": "provider/model",
    "OANDA_API_KEY": "oanda",
    "EIA_API_KEY": "eia",
    "TWITTERAPI_KEY": "tw",
    "COMPANIES_HOUSE_API_KEY": "ch",
    "EDINET_API_KEY": "ed",
    "OPENDART_API_KEY": "od",
    "SEC_USER_AGENT": "test-agent",
    "LOG_LEVEL": "INFO",
}


def _unauthed_request():
    return SimpleNamespace(session={})


def _admin_request():
    return SimpleNamespace(session={"authenticated": True})


class SetupStateTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="setup-state-"))
        self.marker = self.state / "activated.json"
        self.live = {
            "auth.json": self.state / "auth.json",
            "operator.yaml": self.state / "operator.yaml",
            "secrets.env": self.state / "secrets.env",
        }
        self.payload = {
            "auth.json": json.dumps(auth.hash_password(VALID_PASSWORD)),
            "operator.yaml": "llm:\n  default_model: test-model\n",
            "secrets.env": "FRED_API_KEY=old\n",
        }

    def _patches(self):
        return [
            patch.object(setup, "STATE_DIR", self.state),
            patch.object(setup, "ACTIVATION_FILE", self.marker),
            patch.object(setup, "AUTH_FILE", self.live["auth.json"]),
            patch.object(setup, "OPERATOR_FILE", self.live["operator.yaml"]),
            patch.object(setup, "_reload_or_restart", return_value=False),
            patch.object(auth, "STATE_DIR", self.state),
            patch.object(auth, "ACTIVATION_FILE", self.marker),
            patch.object(auth, "AUTH_FILE", self.live["auth.json"]),
            patch.object(auth, "OPERATOR_FILE", self.live["operator.yaml"]),
            patch.object(auth, "SECRETS_FILE", self.live["secrets.env"]),
            patch.object(settings_route, "STATE_DIR", self.state),
            patch.object(settings_route, "SECRETS_FILE", self.live["secrets.env"]),
            patch.object(settings_route, "OPERATOR_CONFIG", self.live["operator.yaml"]),
            patch.object(settings_route, "_reload_or_restart", return_value=False),
            # The full-config candidate gate runs against the real config.yaml
            # (RuntimeConfig); unit tests keep it inert and test it explicitly.
            patch.object(config_module, "validate_candidate", return_value=None),
        ]

    def _commit(self, version_payload=None):
        with setup_state.setup_lock(self.state):
            return setup_state.commit_setup(
                self.state, self.marker, self.live, version_payload or self.payload
            )

    def _is_complete(self):
        return setup_state.validate_committed_state(
            self.state,
            self.marker,
            self.live,
        )

    def test_activate_commits_versioned_state_and_creates_session(self):
        request = _unauthed_request()
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            result = setup.activate(
                setup.ActivationRequest(
                    password=VALID_PASSWORD,
                    profile={"llm": {"default_model": "test-model"}},
                    coverage={"fred": True},
                    secrets={"LLM_API_KEY": "private"},
                ),
                request,
            )
        self.assertTrue(result["activated"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["restart_required"], False)
        marker = json.loads(self.marker.read_text())
        self.assertEqual(marker["version"], 1)
        self.assertEqual(marker["layout"], "versions")
        version_dir = self.state / "versions" / "v1"
        for name in ("auth.json", "operator.yaml", "secrets.env", "manifest.json"):
            self.assertTrue((version_dir / name).exists(), name)
        manifest = json.loads((version_dir / "manifest.json").read_text())
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(
            set(manifest["files"]), {"auth.json", "operator.yaml", "secrets.env"}
        )
        # A single current symlink is the atomic commit point; every consumer
        # path is a stable link through it.
        self.assertTrue((self.state / "current").is_symlink())
        self.assertEqual((self.state / "current").resolve(), version_dir.resolve())
        for name in ("auth.json", "operator.yaml", "secrets.env"):
            self.assertTrue((self.state / name).is_symlink(), name)
            self.assertEqual(os.readlink(self.state / name), f"current/{name}")
            self.assertEqual(
                (self.state / name).resolve(), (version_dir / name).resolve()
            )
        self.assertTrue((self.state / "activated.json").is_symlink())
        self.assertEqual(
            (self.state / "activated.json").resolve(),
            (version_dir / "manifest.json").resolve(),
        )
        self.assertEqual((self.state / "secrets.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((version_dir).stat().st_mode & 0o777, 0o700)
        # LLM_API_KEY is normalized to the canonical OPENROUTER_API_KEY.
        secrets = setup_state.parse_secrets_file(self.live["secrets.env"].read_text())
        self.assertEqual(secrets, {"OPENROUTER_API_KEY": "private"})
        self.assertTrue(request.session["authenticated"])
        self.assertIn("issued_at", request.session)
        self.assertNotIn("csrf", request.session)

    def test_setup_complete_validates_committed_state(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self.assertFalse(auth.setup_complete())
            self._commit()
            self.assertTrue(auth.setup_complete())

            version_dir = self.state / "versions" / "v1"
            # Corrupted content fails validation.
            (version_dir / "secrets.env").unlink()
            self.assertFalse(auth.setup_complete())
            # Restore and corrupt the manifest checksum instead.
            (version_dir / "secrets.env").write_text("FRED_API_KEY=old\n")
            manifest_path = version_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["secrets.env"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest))
            self.assertFalse(auth.setup_complete())

    def test_uncommitted_files_without_marker_are_not_complete(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self.live["auth.json"].write_text(self.payload["auth.json"])
            self.live["operator.yaml"].write_text(self.payload["operator.yaml"])
            self.assertFalse(auth.setup_complete())

    def test_second_activation_is_locked(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            with self.assertRaises(setup.HTTPException) as raised:
                setup.activate(
                    setup.ActivationRequest(password=VALID_PASSWORD),
                    _unauthed_request(),
                )
        self.assertEqual(raised.exception.status_code, 409)

    def test_concurrent_activation_commits_exactly_once(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def run():
            try:
                request = _unauthed_request()
                barrier.wait()
                results.append(
                    setup.activate(
                        setup.ActivationRequest(
                            password=VALID_PASSWORD,
                            profile={"llm": {"default_model": "test-model"}},
                            secrets={},
                        ),
                        request,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        # Enter the patches once, outside the threads, so module attributes
        # are restored exactly once when the threads are done.
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            threads = [threading.Thread(target=run), threading.Thread(target=run)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], setup.HTTPException)
        self.assertEqual(errors[0].status_code, 409)
        self.assertEqual(json.loads(self.marker.read_text())["version"], 1)
        self.assertTrue((self.state / "versions" / "v1").is_dir())
        self.assertFalse((self.state / "versions" / "v2").exists())

    def test_concurrent_profile_updates_serialize_without_lost_updates(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            barrier = threading.Barrier(2)
            results = []

            def run(payload):
                request = _admin_request()
                barrier.wait()
                results.append(
                    setup.update_profile(setup.ProfileUpdateRequest(**payload), request)
                )

            threads = [
                threading.Thread(
                    target=run,
                    args=({"profile": {"llm": {"models": {"default": "model-a"}}}},),
                ),
                threading.Thread(
                    target=run,
                    args=({"profile": {"budgets": {"daily_llm_usd": 12.5}}},),
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(sorted(item["version"] for item in results), [2, 3])
        profile = yaml_load(self.state / "versions" / "v3" / "operator.yaml")
        self.assertEqual(profile["llm"]["models"]["default"], "model-a")
        self.assertEqual(profile["budgets"]["daily_llm_usd"], 12.5)
        self.assertTrue(self._is_complete())

    def test_failed_commit_leaves_prior_live_state(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            setup.activate(
                setup.ActivationRequest(
                    password=VALID_PASSWORD,
                    profile={"llm": {"default_model": "test-model"}},
                    secrets={"FRED_API_KEY": "old"},
                ),
                _unauthed_request(),
            )

            # Inject a failure at the atomic current swap (the commit point)
            # for this update; the initial activation already happened.
            original_flip = setup_state._flip_current
            calls = {"count": 0}

            def failing_flip(current_link, directory):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError("current swap failed")
                return original_flip(current_link, directory)

            with patch.object(setup_state, "_flip_current", side_effect=failing_flip):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.update_profile(
                        setup.ProfileUpdateRequest(
                            secrets={"FRED_API_KEY": "replacement"}
                        ),
                        _admin_request(),
                    )
            self.assertEqual(raised.exception.status_code, 500)

        # Prior live state is intact: current, marker, and every consumer path
        # still resolve to version 1 with the original secret.
        self.assertEqual(
            (self.state / "current").resolve(),
            (self.state / "versions" / "v1").resolve(),
        )
        self.assertEqual(json.loads(self.marker.read_text())["version"], 1)
        self.assertTrue((self.state / "auth.json").is_symlink())
        self.assertEqual(
            (self.state / "auth.json").resolve(),
            (self.state / "versions" / "v1" / "auth.json").resolve(),
        )
        secrets = setup_state.parse_secrets_file(self.live["secrets.env"].read_text())
        self.assertEqual(secrets, {"FRED_API_KEY": "old"})
        self.assertTrue(self._is_complete())

    def test_failed_activation_remains_retryable(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            with patch.object(setup, "commit_setup", side_effect=OSError("disk full")):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(
                            password=VALID_PASSWORD, profile={}, secrets={}
                        ),
                        _unauthed_request(),
                    )
            self.assertEqual(raised.exception.status_code, 500)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.state / "versions").exists())
        self.assertFalse((self.state / "auth.json").exists())
        self.assertFalse(self._is_complete())

    def test_reload_failure_is_reported_truthfully_after_commit(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            with patch.object(setup, "_reload_or_restart", return_value=True):
                result = setup.activate(
                    setup.ActivationRequest(
                        password=VALID_PASSWORD, profile={}, secrets={}
                    ),
                    _unauthed_request(),
                )
        # The commit is durable; the truthfully reported restart requirement
        # covers the in-process configuration reload failure.
        self.assertTrue(result["activated"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(json.loads(self.marker.read_text())["version"], 1)
        self.assertTrue(self._is_complete())

    def test_secret_delete_removes_credential_from_later_state_and_requests(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            setup.activate(
                setup.ActivationRequest(
                    password=VALID_PASSWORD,
                    profile={"llm": {"default_model": "test-model"}},
                    secrets={
                        "FRED_API_KEY": "old",
                        "OANDA_API_KEY": "kept",
                        "OPENROUTER_API_KEY": "sk-live",
                    },
                ),
                _unauthed_request(),
            )
            result = setup.update_profile(
                setup.ProfileUpdateRequest(
                    secrets={"FRED_API_KEY": None, "OPENROUTER_API_KEY": None}
                ),
                _admin_request(),
            )
            self.assertEqual(result["version"], 2)
            # Deletion is a canonical KEY= tombstone: the key is present but
            # empty, so loaders fail closed instead of re-sourcing.
            secrets = setup_state.parse_secrets_file(
                self.live["secrets.env"].read_text()
            )
            self.assertEqual(
                secrets,
                {
                    "OANDA_API_KEY": "kept",
                    "FRED_API_KEY": "",
                    "OPENROUTER_API_KEY": "",
                },
            )
            self.assertEqual(
                setup._read_secrets(),
                {
                    "OANDA_API_KEY": "kept",
                    "FRED_API_KEY": "",
                    "OPENROUTER_API_KEY": "",
                },
            )
            self.assertIn("FRED_API_KEY=\n", self.live["secrets.env"].read_text())
            # Managed resolution is file-authoritative after activation: a
            # stale process-environment value is never reused.
            with mock.patch.dict(os.environ, {"FRED_API_KEY": "stale-env"}):
                self.assertEqual(settings_route.managed_secret("FRED_API_KEY"), "")
                self.assertEqual(settings_route.managed_secret("OANDA_API_KEY"), "kept")
            # A subsequent connection test with no supplied key fails closed
            # before any outbound call, even with a stale env value.
            with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "stale-env"}):
                with mock.patch("httpx.Client") as client_mock:
                    with self.assertRaises(setup.HTTPException) as raised:
                        setup.test_connection(
                            setup.TestConnectionRequest(),
                            _admin_request(),
                        )
                    self.assertEqual(raised.exception.status_code, 400)
                    client_mock.assert_not_called()

    def test_malformed_secrets_are_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            setup.ActivationRequest(secrets={"FRED_API_KEY": 123})
        with self.assertRaises(pydantic.ValidationError):
            setup.ActivationRequest(secrets={"FRED_API_KEY": ["nested"]})
        with self.assertRaises(pydantic.ValidationError):
            setup.ActivationRequest(coverage={"unknown_source": True})
        with self.assertRaises(pydantic.ValidationError):
            setup.ActivationRequest(coverage={"fred": "yes"})
        with self.assertRaises(pydantic.ValidationError):
            setup.ActivationRequest(password=12345)
        with self.assertRaises(pydantic.ValidationError):
            setup.ActivationRequest(unexpected_field=True)

        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            # Unknown secret names and control characters are rejected as 422.
            with self.assertRaises(setup.HTTPException) as raised:
                setup.activate(
                    setup.ActivationRequest(
                        password=VALID_PASSWORD, secrets={"EVIL_KEY": "value"}
                    ),
                    _unauthed_request(),
                )
            self.assertEqual(raised.exception.status_code, 422)
            with self.assertRaises(setup.HTTPException) as raised:
                setup.activate(
                    setup.ActivationRequest(
                        password=VALID_PASSWORD,
                        secrets={"FRED_API_KEY": "line1\nline2"},
                    ),
                    _unauthed_request(),
                )
            self.assertEqual(raised.exception.status_code, 422)
        self.assertFalse((self.state / "versions").exists())

    def test_request_string_fields_are_length_bounded(self):
        """Oversized password/token/url strings are rejected at the model
        boundary (422) so scrypt/compare/parse cannot be driven into
        unbounded work, and the rejected value is never echoed."""
        oversized_password = "x" * 2000
        with self.assertRaises(pydantic.ValidationError) as raised:
            setup.ActivationRequest(password=oversized_password)
        self.assertNotIn(oversized_password, str(raised.exception))
        with self.assertRaises(pydantic.ValidationError):
            setup.ActivationRequest(token="x" * 300)
        with self.assertRaises(pydantic.ValidationError):
            setup.LoginRequest(password="x" * 2000)
        with self.assertRaises(pydantic.ValidationError):
            setup.TestConnectionRequest(api_key="x" * 5000)
        with self.assertRaises(pydantic.ValidationError):
            setup.TestConnectionRequest(token="x" * 300)
        # Unknown fields are rejected (the runtime test surface is fixed to
        # the canonical OpenRouter origin; no arbitrary base_url input).
        with self.assertRaises(pydantic.ValidationError):
            setup.TestConnectionRequest(base_url="https://example.invalid/v1")
        # Boundary values just inside the limit are accepted.
        model = setup.ActivationRequest(
            password="a sufficiently long password", token="x" * 256
        )
        self.assertEqual(len(model.token), 256)

    def test_operator_settings_update_preserves_profile_and_commits_version(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            setup.activate(
                setup.ActivationRequest(
                    password=VALID_PASSWORD,
                    profile={
                        "llm": {"default_model": "test-model"},
                        "watchlist": {"trading": ["EURUSD"]},
                    },
                    coverage={"fred": True},
                    secrets={"FRED_API_KEY": "old"},
                ),
                _unauthed_request(),
            )
            result = settings_route.update_operator_settings(
                {
                    "llm": {"models": {"default": "new-model"}},
                    "daily_budget_usd": 5.0,
                    "secrets": {"FRED_API_KEY": "new"},
                }
            )
        self.assertEqual(result["saved"], True)
        self.assertIsInstance(result["restart_required"], bool)
        profile = yaml_load(self.state / "versions" / "v2" / "operator.yaml")
        self.assertEqual(profile["llm"]["models"]["default"], "new-model")
        # Sections outside the settings form are preserved, not clobbered.
        self.assertEqual(profile["collectors"]["fred"]["enabled"], True)
        self.assertEqual(profile["watchlist"]["trading"], ["EURUSD"])
        secrets = setup_state.parse_secrets_file(self.live["secrets.env"].read_text())
        self.assertEqual(secrets, {"FRED_API_KEY": "new"})
        self.assertEqual(json.loads(self.marker.read_text())["version"], 2)

    def test_status_reports_committed_version(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self.assertEqual(setup.status()["setup_complete"], False)
            self.assertEqual(setup.status()["version"], None)
            self._commit()
            self.assertEqual(setup.status()["setup_complete"], True)
            self.assertEqual(setup.status()["version"], 1)

    def test_tampered_pointer_is_not_complete(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            self._commit(
                {
                    "auth.json": self.payload["auth.json"],
                    "operator.yaml": "llm:\n  default_model: second\n",
                    "secrets.env": "FRED_API_KEY=second\n",
                }
            )
            self.assertTrue(auth.setup_complete())
            # Split-brain: the marker (manifest link) is replaced by a real
            # file naming an older version while current points at v2.
            self.marker.unlink()
            self.marker.write_text(json.dumps({"version": 1, "layout": "versions"}))
            self.assertFalse(auth.setup_complete())

    def test_tampered_root_link_is_not_complete(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            self._commit(
                {
                    "auth.json": self.payload["auth.json"],
                    "operator.yaml": "llm:\n  default_model: second\n",
                    "secrets.env": "FRED_API_KEY=second\n",
                }
            )
            self.assertTrue(auth.setup_complete())
            # Split-brain: a root consumer path resolves into the previous
            # version while current points at v2.
            auth_link = self.live["auth.json"]
            auth_link.unlink()
            os.symlink("versions/v1/auth.json", auth_link)
            self.assertFalse(auth.setup_complete())

    def test_manual_current_rollback_is_still_complete(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            self._commit(
                {
                    "auth.json": self.payload["auth.json"],
                    "operator.yaml": "llm:\n  default_model: second\n",
                    "secrets.env": "FRED_API_KEY=second\n",
                }
            )
            self.assertTrue(auth.setup_complete())
            # Pointing current at the previous version is a consistent
            # operator rollback: the marker follows through current.
            current = self.state / "current"
            current.unlink()
            os.symlink("versions/v1", current)
            self.assertTrue(auth.setup_complete())

    def test_first_activation_failure_at_flip_leaves_no_committed_state(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            with patch.object(
                setup_state, "_flip_current", side_effect=OSError("swap failed")
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(
                            password=VALID_PASSWORD, profile={}, secrets={}
                        ),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 500)
            # Nothing committed: no pointer, no marker, no committed state.
            self.assertFalse((self.state / "current").is_symlink())
            self.assertFalse(self.marker.exists())
            self.assertFalse(auth.setup_complete())
            # The retry (unpatched) succeeds atomically.
            result = setup.activate(
                setup.ActivationRequest(
                    password=VALID_PASSWORD, profile={}, secrets={}
                ),
                _unauthed_request(),
            )
            self.assertTrue(result["activated"])
            self.assertTrue((self.state / "current").is_symlink())
            self.assertTrue(auth.setup_complete())

    def test_prune_keeps_actual_previous_committed_version(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            self._commit(
                {
                    "auth.json": self.payload["auth.json"],
                    "operator.yaml": "llm:\n  default_model: second\n",
                    "secrets.env": "FRED_API_KEY=second\n",
                }
            )
            # A failed commit leaves an orphaned staged version (v3).
            with patch.object(
                setup_state, "_flip_current", side_effect=OSError("swap failed")
            ):
                with self.assertRaises(OSError):
                    with setup_state.setup_lock(self.state):
                        setup_state.commit_setup(
                            self.state,
                            self.marker,
                            self.live,
                            {
                                "auth.json": self.payload["auth.json"],
                                "operator.yaml": "llm:\n  default_model: third\n",
                                "secrets.env": "FRED_API_KEY=third\n",
                            },
                        )
            self.assertTrue((self.state / "versions" / "v3").is_dir())
            # The next successful commit (v4) prunes by the actual previous
            # committed version (v2), keeping the rollback target.
            with setup_state.setup_lock(self.state):
                setup_state.commit_setup(
                    self.state,
                    self.marker,
                    self.live,
                    {
                        "auth.json": self.payload["auth.json"],
                        "operator.yaml": "llm:\n  default_model: fourth\n",
                        "secrets.env": "FRED_API_KEY=fourth\n",
                    },
                )
            remaining = sorted(p.name for p in (self.state / "versions").iterdir())
            self.assertEqual(remaining, ["v2", "v4"])
            self.assertTrue(auth.setup_complete())

    def test_oversized_or_deep_profile_is_rejected_with_422(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            deep = {}
            node = deep
            for _ in range(20):
                node["nested"] = {}
                node = node["nested"]
            with self.assertRaises(setup.HTTPException) as raised:
                setup.activate(
                    setup.ActivationRequest(
                        password=VALID_PASSWORD, profile=deep, secrets={}
                    ),
                    _unauthed_request(),
                )
            self.assertEqual(raised.exception.status_code, 422)
            huge = {"padding": "x" * (setup._PROFILE_SIZE_LIMIT + 1)}
            with self.assertRaises(setup.HTTPException) as raised:
                setup.update_profile(
                    setup.ProfileUpdateRequest(profile=huge), _admin_request()
                )
            self.assertEqual(raised.exception.status_code, 422)
        self.assertFalse((self.state / "versions").exists())

    def test_invalid_candidate_is_rejected_before_commit(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            # The full-configuration gate rejects the staged candidate; the
            # pointer must not move and the prior state stays live.
            with patch.object(
                config_module,
                "validate_candidate",
                side_effect=ValueError("cross-field conflict"),
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.update_profile(
                        setup.ProfileUpdateRequest(
                            secrets={"FRED_API_KEY": "replacement"}
                        ),
                        _admin_request(),
                    )
                self.assertEqual(raised.exception.status_code, 422)
            self.assertEqual(json.loads(self.marker.read_text())["version"], 1)
            secrets = setup_state.parse_secrets_file(
                self.live["secrets.env"].read_text()
            )
            self.assertEqual(secrets["FRED_API_KEY"], "old")
            self.assertTrue(auth.setup_complete())

    def test_invalid_candidate_keeps_first_activation_incomplete(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            with patch.object(
                config_module,
                "validate_candidate",
                side_effect=ValueError("cross-field conflict"),
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(
                            password=VALID_PASSWORD, profile={}, secrets={}
                        ),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 422)
            # Nothing was committed: no pointer, no marker, not complete.
            self.assertFalse((self.state / "current").exists())
            self.assertFalse(self.marker.exists())
            self.assertFalse(auth.setup_complete())

    def test_fsync_orders_staged_files_before_flip_and_state_dir_after(self):
        order = []
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            with (
                patch.object(
                    setup_state, "_fsync_file", side_effect=self._record(order, "file")
                ),
                patch.object(
                    setup_state, "_fsync_dir", side_effect=self._record(order, "dir")
                ),
                patch.object(
                    setup_state,
                    "_flip_current",
                    side_effect=self._record(order, "flip"),
                ),
            ):
                self._commit()
        flip_at = next(i for i, event in enumerate(order) if event[0] == "flip")
        self.assertTrue(
            all(order.index(event) < flip_at for event in order if event[0] == "file"),
            order,
        )
        # The state directory is fsynced after the flip to persist the pointer.
        self.assertTrue(
            any(
                event[0] == "dir" and index > flip_at
                for index, event in enumerate(order)
            ),
            order,
        )
        self.assertTrue(self._is_complete())

    def test_fsync_failure_aborts_commit_and_keeps_prior_state(self):
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            with patch.object(
                setup_state, "_fsync_file", side_effect=OSError("disk full")
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.update_profile(
                        setup.ProfileUpdateRequest(
                            secrets={"FRED_API_KEY": "replacement"}
                        ),
                        _admin_request(),
                    )
                self.assertEqual(raised.exception.status_code, 500)
            self.assertEqual(json.loads(self.marker.read_text())["version"], 1)
            secrets = setup_state.parse_secrets_file(
                self.live["secrets.env"].read_text()
            )
            self.assertEqual(secrets["FRED_API_KEY"], "old")
            self.assertTrue(auth.setup_complete())

    def test_directory_fsync_filter_propagates_storage_failures(self):
        """Unsupported directory-fsync errnos are skipped; real storage
        failures (EIO/ENOSPC) propagate instead of being swallowed."""
        with patch.object(
            setup_state.os, "open", side_effect=OSError(errno.EINVAL, "unsupported")
        ):
            setup_state._fsync_dir(self.state)  # tolerated
        with patch.object(
            setup_state.os, "open", side_effect=OSError(errno.ENOTSUP, "unsupported")
        ):
            setup_state._fsync_dir(self.state)  # tolerated
        with patch.object(
            setup_state.os, "open", side_effect=OSError(errno.EBADF, "bad fd")
        ):
            setup_state._fsync_dir(self.state)  # tolerated
        with patch.object(
            setup_state.os, "open", side_effect=OSError(errno.EIO, "I/O error")
        ):
            with self.assertRaises(OSError) as raised:
                setup_state._fsync_dir(self.state)
            self.assertEqual(raised.exception.errno, errno.EIO)
        with patch.object(
            setup_state.os, "open", side_effect=OSError(errno.ENOSPC, "no space")
        ):
            with self.assertRaises(OSError) as raised:
                setup_state._fsync_dir(self.state)
            self.assertEqual(raised.exception.errno, errno.ENOSPC)

    def test_eio_before_flip_keeps_prior_live_state(self):
        """A real storage failure (EIO) during pre-flip directory fsync must
        propagate, abort the commit, and keep the prior state live."""
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            flipped = {"yes": False}
            original_flip = setup_state._flip_current

            def flip_recorder(link, directory):
                flipped["yes"] = True
                return original_flip(link, directory)

            def fail_eio(path):
                if not flipped["yes"] and path == self.state:
                    raise OSError(errno.EIO, "I/O error")
                return original_fsync_dir(path)

            original_fsync_dir = setup_state._fsync_dir
            with (
                patch.object(setup_state, "_flip_current", side_effect=flip_recorder),
                patch.object(setup_state, "_fsync_dir", side_effect=fail_eio),
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.update_profile(
                        setup.ProfileUpdateRequest(
                            secrets={"FRED_API_KEY": "replacement"}
                        ),
                        _admin_request(),
                    )
                self.assertEqual(raised.exception.status_code, 500)
                self.assertIn(
                    "previous settings remain active",
                    raised.exception.detail,
                )
            # The pointer never moved: prior state is live and complete.
            self.assertEqual(json.loads(self.marker.read_text())["version"], 1)
            secrets = setup_state.parse_secrets_file(
                self.live["secrets.env"].read_text()
            )
            self.assertEqual(secrets["FRED_API_KEY"], "old")
            self.assertTrue(auth.setup_complete())

    def test_eio_after_flip_reports_committed_truthfully(self):
        """A storage failure AFTER the commit point must not claim a safe
        retry: the update is already live, only its durability is unknown."""
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            flipped = {"yes": False}
            original_flip = setup_state._flip_current

            def flip_recorder(link, directory):
                flipped["yes"] = True
                return original_flip(link, directory)

            def fail_after_flip(path):
                if flipped["yes"]:
                    raise OSError(errno.EIO, "I/O error")
                return original_fsync_dir(path)

            original_fsync_dir = setup_state._fsync_dir
            with (
                patch.object(setup_state, "_flip_current", side_effect=flip_recorder),
                patch.object(setup_state, "_fsync_dir", side_effect=fail_after_flip),
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.update_profile(
                        setup.ProfileUpdateRequest(
                            secrets={"FRED_API_KEY": "replacement"}
                        ),
                        _admin_request(),
                    )
                self.assertEqual(raised.exception.status_code, 500)
                self.assertIn(
                    "durability could not be confirmed", raised.exception.detail
                )
                self.assertNotIn("safe", raised.exception.detail.lower())
            # The commit point passed: the new version is live and complete.
            self.assertEqual(json.loads(self.marker.read_text())["version"], 2)
            secrets = setup_state.parse_secrets_file(
                self.live["secrets.env"].read_text()
            )
            self.assertEqual(secrets["FRED_API_KEY"], "replacement")
            self.assertTrue(auth.setup_complete())

    def _record(self, events, kind):
        originals = {
            "file": setup_state._fsync_file,
            "dir": setup_state._fsync_dir,
            "flip": setup_state._flip_current,
        }
        original = originals[kind]

        def recorder(*args, **_kwargs):
            events.append((kind, getattr(args[0], "name", str(args[0]))))
            return original(*args, **_kwargs)

        return recorder

    def test_flip_race_never_reports_committed_state_incomplete(self):
        """A concurrent pointer flip must not split setup_complete checks:
        every resolution names one complete committed version."""
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self._commit()
            self._commit(
                {
                    "auth.json": self.payload["auth.json"],
                    "operator.yaml": "llm:\n  default_model: second\n",
                    "secrets.env": "FRED_API_KEY=second\n",
                }
            )
            self.assertTrue(auth.setup_complete())
            stop = threading.Event()
            failures = []
            v1 = self.state / "versions" / "v1"
            v2 = self.state / "versions" / "v2"
            current = self.state / "current"

            def flipper():
                while not stop.is_set():
                    for target in (v1, v2):
                        setup_state._flip_current(current, target)

            def reader():
                while not stop.is_set():
                    if not auth.setup_complete():
                        failures.append("setup_complete returned False")

            threads = [
                threading.Thread(target=flipper),
                threading.Thread(target=reader),
                threading.Thread(target=reader),
            ]
            for thread in threads:
                thread.start()
            threading.Event().wait(0.25)
            stop.set()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            self.assertTrue(auth.setup_complete())

    def test_connection_pre_and_post_activation_bootstrap_boundary(self):
        """/api/setup/test-connection is bootstrap-public: pre-activation it
        reaches the route (gated by the bootstrap token, not a global 401);
        post-activation it requires a session and browser CSRF."""
        from fastapi.testclient import TestClient

        config = {"logging": {"level": "INFO"}}
        with patch.object(config_module, "load_config", return_value=config):
            import main
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (
                patch.object(auth, "STATE_DIR", state),
                patch.object(auth, "AUTH_FILE", state / "auth.json"),
                patch.object(auth, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(auth, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(auth, "SECRETS_FILE", state / "secrets.env"),
                patch.object(setup, "STATE_DIR", state),
                patch.object(setup, "AUTH_FILE", state / "auth.json"),
                patch.object(setup, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(setup, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(main, "STATE_DIR", state),
                patch.object(main, "AUTH_FILE", state / "auth.json"),
                patch.object(main, "OPERATOR_FILE", state / "operator.yaml"),
                patch.object(main, "ACTIVATION_FILE", state / "activated.json"),
                patch.object(main, "load_config", return_value=config),
                patch.object(config_module, "validate_candidate", return_value=None),
                patch.object(setup, "managed_secret", return_value=""),
            ):
                app = main.create_app()
                client = TestClient(app, base_url="https://testserver")
                payload = {}

                # Pre-activation, production, no token configured: the request
                # reaches the route's bootstrap-token gate (503), not a 401.
                with patch.dict(
                    os.environ,
                    {"DEPLOYMENT_MODE": "production", "SETUP_TOKEN": ""},
                    clear=False,
                ):
                    response = client.post("/api/setup/test-connection", json=payload)
                    self.assertEqual(response.status_code, 503, response.text)

                # Configured token: 403 without, then fails closed on the key.
                with patch.dict(
                    os.environ,
                    {
                        "DEPLOYMENT_MODE": "production",
                        "SETUP_TOKEN": VALID_SETUP_TOKEN,
                    },
                    clear=False,
                ):
                    with mock.patch("httpx.Client") as client_mock:
                        denied = client.post("/api/setup/test-connection", json=payload)
                        self.assertEqual(denied.status_code, 403, denied.text)
                        no_key = client.post(
                            "/api/setup/test-connection",
                            json={**payload, "token": VALID_SETUP_TOKEN},
                        )
                        self.assertEqual(no_key.status_code, 400, no_key.text)
                        client_mock.assert_not_called()

                # Test mode: token optional; fails closed on the missing key.
                with mock.patch("httpx.Client") as client_mock:
                    response = client.post("/api/setup/test-connection", json=payload)
                    self.assertEqual(response.status_code, 400, response.text)
                    client_mock.assert_not_called()

                # Activate: the session is now authenticated.
                activate = client.post(
                    "/api/setup/activate",
                    json={
                        "password": VALID_PASSWORD,
                        "profile": {"llm": {"models": {"default": "test-model"}}},
                        "secrets": {},
                    },
                )
                self.assertEqual(activate.status_code, 200, activate.text)
                self.assertTrue(auth.setup_complete())

                # Post-activation without a session: route rejects with 401.
                anonymous = TestClient(app, base_url="https://testserver")
                no_session = anonymous.post("/api/setup/test-connection", json=payload)
                self.assertEqual(no_session.status_code, 401, no_session.text)

                # Post-activation with a session: browser flow needs CSRF.
                with mock.patch("httpx.Client") as client_mock:
                    browser = client.post(
                        "/api/setup/test-connection",
                        json=payload,
                        headers={"Origin": "https://testserver"},
                    )
                    self.assertEqual(browser.status_code, 403, browser.text)
                    client_mock.assert_not_called()

    def test_markerless_files_are_never_converted_or_activated(self):
        """Valid-looking root leftovers WITHOUT an activation marker are
        uncommitted state: a failing activation must not activate them."""
        with ExitStack() as stack:
            for entry in self._patches():
                stack.enter_context(entry)
            self.live["auth.json"].write_text(self.payload["auth.json"])
            self.live["operator.yaml"].write_text(
                "llm:\n  models:\n    default: leftover\n"
            )
            with patch.object(
                config_module,
                "validate_candidate",
                side_effect=ValueError("bad candidate"),
            ):
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(
                            password=VALID_PASSWORD, profile={}, secrets={}
                        ),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 422)
            # Nothing was committed: no pointer, no marker, not complete.
            self.assertFalse((self.state / "current").exists())
            self.assertFalse(self.marker.exists())
            self.assertFalse(auth.setup_complete())
            # A retry with a valid candidate commits the intended payload as
            # v1; the leftovers are never converted into a committed version.
            res = setup.activate(
                setup.ActivationRequest(
                    password=VALID_PASSWORD,
                    profile={"llm": {"models": {"default": "intended"}}},
                    secrets={},
                ),
                _unauthed_request(),
            )
            self.assertEqual(res["version"], 1)
            profile = yaml_load(self.state / "versions" / "v1" / "operator.yaml")
            self.assertEqual(profile["llm"]["models"]["default"], "intended")
            self.assertTrue(auth.setup_complete())

    def test_activation_with_unsupported_profile_fields_is_rejected(self):
        """Legacy/unsupported llm profile keys fail strict staged validation
        with 422 and nothing is committed (no silent canonicalization)."""
        if not hasattr(config_module, "validate_candidate"):
            self.skipTest("config.validate_candidate not yet available")
        real_validator = config_module.validate_candidate
        with patch.dict(os.environ, REAL_CONFIG_ENV, clear=False):
            with ExitStack() as stack:
                for entry in self._patches():
                    stack.enter_context(entry)
                stack.enter_context(
                    patch.object(
                        config_module, "validate_candidate", new=real_validator
                    )
                )
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(
                            password=VALID_PASSWORD,
                            profile={
                                "llm": {
                                    "default_model": "legacy",
                                    "base_url": "https://x.example",
                                    "reasoning_effort": "high",
                                }
                            },
                            secrets={
                                "FRED_API_KEY": "old",
                                "OPENROUTER_API_KEY": "sk",
                                "OANDA_API_KEY": "oanda",
                            },
                        ),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 422)
            self.assertFalse((self.state / "current").exists())
            self.assertFalse(self.marker.exists())
            self.assertFalse(self._is_complete())

    def test_cross_field_invalid_candidate_is_rejected_end_to_end(self):
        """Full-config gate: removing the globally required model credential
        must fail the staged candidate (422), keep the prior state, and leave
        a first activation incomplete."""
        if not hasattr(config_module, "validate_candidate"):
            self.skipTest("config.validate_candidate not yet available")
        real_validator = config_module.validate_candidate
        with patch.dict(os.environ, REAL_CONFIG_ENV, clear=False):
            with ExitStack() as stack:
                for entry in self._patches():
                    stack.enter_context(entry)
                # Override the inert unit-test no-op with the real validator.
                stack.enter_context(
                    patch.object(
                        config_module, "validate_candidate", new=real_validator
                    )
                )
                # A candidate with all no-default managed keys set is valid.
                self._commit(
                    {
                        "auth.json": self.payload["auth.json"],
                        "operator.yaml": ("llm:\n  models:\n    default: test-model\n"),
                        "secrets.env": (
                            "FRED_API_KEY=old\n"
                            "OPENROUTER_API_KEY=sk\n"
                            "OANDA_API_KEY=oanda\n"
                        ),
                    }
                )
                # OpenRouter has no blank default because every analytical
                # processor depends on it; a tombstone invalidates the staged
                # candidate before the atomic pointer swap.
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.update_profile(
                        setup.ProfileUpdateRequest(
                            secrets={"OPENROUTER_API_KEY": None}
                        ),
                        _admin_request(),
                    )
                self.assertEqual(raised.exception.status_code, 422)
                secrets = setup_state.parse_secrets_file(
                    self.live["secrets.env"].read_text()
                )
                self.assertEqual(secrets["OPENROUTER_API_KEY"], "sk")
                self.assertEqual(json.loads(self.marker.read_text())["version"], 1)
                self.assertTrue(auth.setup_complete())


class SetupTokenBoundaryTests(unittest.TestCase):
    """Production bootstrap authentication boundary for SETUP_TOKEN."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="setup-token-"))
        self.marker = self.state / "activated.json"
        self.live = {
            "auth.json": self.state / "auth.json",
            "operator.yaml": self.state / "operator.yaml",
            "secrets.env": self.state / "secrets.env",
        }

    def test_setup_page_collects_and_submits_bootstrap_token(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient
        from routes.views import setup as setup_view

        app = FastAPI()
        app.state.templates = Jinja2Templates(
            directory=Path(__file__).resolve().parents[1] / "templates"
        )
        app.include_router(setup_view.router)
        with (
            patch.object(setup_view, "setup_complete", return_value=False),
            # Setup remains available in production until activation completes.
            patch.dict(
                os.environ,
                {"DEPLOYMENT_MODE": "production"},
                clear=False,
            ),
        ):
            response = TestClient(app).get("/setup")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="setup-token"', response.text)
        self.assertIn(
            "token: document.getElementById('setup-token').value", response.text
        )

    def _patches(self):
        return [
            patch.object(setup, "STATE_DIR", self.state),
            patch.object(setup, "ACTIVATION_FILE", self.marker),
            patch.object(setup, "AUTH_FILE", self.live["auth.json"]),
            patch.object(setup, "OPERATOR_FILE", self.live["operator.yaml"]),
            patch.object(auth, "STATE_DIR", self.state),
            patch.object(auth, "ACTIVATION_FILE", self.marker),
            patch.object(auth, "AUTH_FILE", self.live["auth.json"]),
            patch.object(auth, "OPERATOR_FILE", self.live["operator.yaml"]),
            patch.object(auth, "SECRETS_FILE", self.live["secrets.env"]),
            patch.object(setup, "_reload_or_restart", return_value=False),
            patch.object(setup, "managed_secret", return_value=""),
            patch.object(config_module, "validate_candidate", return_value=None),
        ]

    def test_production_requires_strong_configured_token(self):
        cases = [
            {"SETUP_TOKEN": "", "expected": 503},
            {"SETUP_TOKEN": "short", "expected": 503},
            {"SETUP_TOKEN": "change-me-please-this-is-long", "expected": 503},
            {"SETUP_TOKEN": "replace-with-your-own-value-123456", "expected": 503},
            # Low-diversity patterns pass the length bounds but encode far
            # less than a generated secret: rejected.
            {"SETUP_TOKEN": "ab" * 16, "expected": 503},
            {"SETUP_TOKEN": "ab" * 32, "expected": 503},
            {"SETUP_TOKEN": "a" * 64, "expected": 503},
        ]
        for case in cases:
            token, expected = case["SETUP_TOKEN"], case["expected"]
            with self.subTest(token=token[:24]):
                with patch.dict(
                    os.environ,
                    {"DEPLOYMENT_MODE": "production", "SETUP_TOKEN": token},
                    clear=False,
                ):
                    with ExitStack() as stack:
                        for entry in self._patches():
                            stack.enter_context(entry)
                        with self.assertRaises(setup.HTTPException) as raised:
                            setup.activate(
                                setup.ActivationRequest(password=VALID_PASSWORD),
                                _unauthed_request(),
                            )
                self.assertEqual(raised.exception.status_code, expected)

    def test_generated_style_tokens_are_accepted(self):
        import secrets as secrets_module

        generated = secrets_module.token_urlsafe(48)  # 64 chars, >=256 bits
        self.assertTrue(setup._valid_bootstrap_token(generated))
        self.assertTrue(setup._valid_bootstrap_token(VALID_SETUP_TOKEN))
        # 64 characters with 16 distinct characters estimate exactly 256 bits.
        self.assertTrue(setup._valid_bootstrap_token("0123456789abcdef" * 4))
        self.assertFalse(setup._valid_bootstrap_token("ab" * 32))
        self.assertFalse(setup._valid_bootstrap_token("a" * 64))

    def test_production_rejects_missing_or_wrong_token(self):
        import secrets as secrets_module

        strong_keys = {
            "SESSION_SIGNING_KEY": secrets_module.token_urlsafe(48),
            "CSRF_SIGNING_KEY": secrets_module.token_urlsafe(48),
        }
        with patch.dict(
            os.environ,
            {
                "DEPLOYMENT_MODE": "production",
                "SETUP_TOKEN": VALID_SETUP_TOKEN,
                **strong_keys,
            },
            clear=False,
        ):
            with ExitStack() as stack:
                for entry in self._patches():
                    stack.enter_context(entry)
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(password=VALID_PASSWORD),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 403)
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(
                            password=VALID_PASSWORD, token="wrong-token-value-123456"
                        ),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 403)
                result = setup.activate(
                    setup.ActivationRequest(
                        password=VALID_PASSWORD, token=VALID_SETUP_TOKEN
                    ),
                    _unauthed_request(),
                )
                self.assertTrue(result["activated"])

    def test_demo_mode_token_is_optional_but_gates_when_configured(self):
        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "demo", "SETUP_TOKEN": VALID_SETUP_TOKEN},
            clear=False,
        ):
            with ExitStack() as stack:
                for entry in self._patches():
                    stack.enter_context(entry)
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(password=VALID_PASSWORD),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 403)

        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "demo", "SETUP_TOKEN": ""},
            clear=False,
        ):
            with ExitStack() as stack:
                for entry in self._patches():
                    stack.enter_context(entry)
                result = setup.activate(
                    setup.ActivationRequest(password=VALID_PASSWORD),
                    _unauthed_request(),
                )
            self.assertTrue(result["activated"])

    def test_configured_token_over_max_is_rejected(self):
        """A configured SETUP_TOKEN beyond the request bound (256) is invalid:
        it could never be submitted, so activation is refused with 503."""
        long_token = "a" * 32 + "B" * 269  # 301 chars, no placeholder fragments
        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "production", "SETUP_TOKEN": long_token},
            clear=False,
        ):
            with ExitStack() as stack:
                for entry in self._patches():
                    stack.enter_context(entry)
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.activate(
                        setup.ActivationRequest(password=VALID_PASSWORD),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 503)

    def test_production_test_connection_requires_bootstrap_token_pre_activation(
        self,
    ):
        # Unauthenticated outbound probes must not be possible before setup.
        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "production", "SETUP_TOKEN": ""},
            clear=False,
        ):
            with ExitStack() as stack:
                for entry in self._patches():
                    stack.enter_context(entry)
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.test_connection(
                        setup.TestConnectionRequest(), _unauthed_request()
                    )
                self.assertEqual(raised.exception.status_code, 503)

        with patch.dict(
            os.environ,
            {"DEPLOYMENT_MODE": "production", "SETUP_TOKEN": VALID_SETUP_TOKEN},
            clear=False,
        ):
            with ExitStack() as stack:
                for entry in self._patches():
                    stack.enter_context(entry)
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.test_connection(
                        setup.TestConnectionRequest(), _unauthed_request()
                    )
                self.assertEqual(raised.exception.status_code, 403)
                with self.assertRaises(setup.HTTPException) as raised:
                    setup.test_connection(
                        setup.TestConnectionRequest(token="wrong-token-value-123456"),
                        _unauthed_request(),
                    )
                self.assertEqual(raised.exception.status_code, 403)
                # A valid token passes the gate; with no credential the probe
                # fails closed before any outbound call.
                with mock.patch("httpx.Client") as client_mock:
                    with self.assertRaises(setup.HTTPException) as raised:
                        setup.test_connection(
                            setup.TestConnectionRequest(token=VALID_SETUP_TOKEN),
                            _unauthed_request(),
                        )
                    self.assertEqual(raised.exception.status_code, 400)
                    client_mock.assert_not_called()
                # An authenticated session pre-activation needs no token.
                with mock.patch("httpx.Client") as client_mock:
                    with self.assertRaises(setup.HTTPException) as raised:
                        setup.test_connection(
                            setup.TestConnectionRequest(), _admin_request()
                        )
                    self.assertEqual(raised.exception.status_code, 400)
                    client_mock.assert_not_called()


class SetupCsrfExemptionTests(unittest.TestCase):
    """After activation, setup mutations must not be CSRF-exempt.

    Depends on the main.py exemption set {/api/login, /api/setup/activate}
    (AuthSecurity's slice). A browser-origin profile update without a CSRF
    token is rejected; with the token it succeeds.
    """

    def _app(self, state, config=None):
        from fastapi.testclient import TestClient

        config = config or {
            "logging": {"level": "INFO"},
            "llm": {
                "base_url": "https://example.invalid/v1",
                "default_model": "test-model",
                "reasoning_effort": "high",
            },
            "budgets": {"daily_llm_usd": 1.0},
            "collectors": {},
        }
        with patch.object(config_module, "load_config", return_value=config):
            import main
        stack = ExitStack()
        for patcher in (
            patch.object(auth, "STATE_DIR", state),
            patch.object(auth, "AUTH_FILE", state / "auth.json"),
            patch.object(auth, "OPERATOR_FILE", state / "operator.yaml"),
            patch.object(auth, "ACTIVATION_FILE", state / "activated.json"),
            patch.object(auth, "SECRETS_FILE", state / "secrets.env"),
            patch.object(main, "STATE_DIR", state),
            patch.object(main, "AUTH_FILE", state / "auth.json"),
            patch.object(main, "OPERATOR_FILE", state / "operator.yaml"),
            patch.object(main, "ACTIVATION_FILE", state / "activated.json"),
            patch.object(main, "load_config", return_value=config),
            patch.object(config_module, "validate_candidate", return_value=None),
            patch.object(setup, "STATE_DIR", state),
            patch.object(setup, "AUTH_FILE", state / "auth.json"),
            patch.object(setup, "OPERATOR_FILE", state / "operator.yaml"),
            patch.object(setup, "ACTIVATION_FILE", state / "activated.json"),
            patch.object(setup, "_reload_or_restart", return_value=False),
        ):
            stack.enter_context(patcher)
        return stack, TestClient(main.create_app(), base_url="https://testserver")

    def test_profile_update_requires_csrf_after_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, client = self._app(Path(directory))
            with stack:
                activate = client.post(
                    "/api/setup/activate",
                    json={
                        "password": VALID_PASSWORD,
                        "profile": {"llm": {"models": {"default": "test-model"}}},
                        "secrets": {},
                    },
                )
                self.assertEqual(activate.status_code, 200)
                self.assertEqual(client.get("/api/setup/status").status_code, 200)
                csrf = client.cookies.get("csrf-token")
                self.assertIsNotNone(csrf)

                browser_headers = {
                    "Origin": "https://testserver",
                    "Content-Type": "application/json",
                }
                denied = client.put(
                    "/api/setup/profile",
                    json={"profile": {"llm": {"models": {"default": "attacker"}}}},
                    headers=browser_headers,
                )
                self.assertEqual(denied.status_code, 403)
                allowed = client.put(
                    "/api/setup/profile",
                    json={"profile": {"llm": {"models": {"default": "operator"}}}},
                    headers={**browser_headers, "X-CSRF-Token": csrf},
                )
                self.assertEqual(allowed.status_code, 200)

    def test_login_and_activate_return_the_middleware_csrf_token(self):
        """Double-submit contract: the returned csrf_token equals the
        csrf-token cookie set on the same login/activate response, and a
        valid-but-different token is rejected (header != cookie)."""
        with tempfile.TemporaryDirectory() as directory:
            stack, client = self._app(Path(directory))
            with stack:
                activate = client.post(
                    "/api/setup/activate",
                    json={
                        "password": VALID_PASSWORD,
                        "profile": {"llm": {"models": {"default": "test-model"}}},
                        "secrets": {},
                    },
                )
                self.assertEqual(activate.status_code, 200, activate.text)
                self.assertEqual(
                    activate.json()["csrf_token"],
                    client.cookies.get("csrf-token"),
                )
                login = client.post(
                    "/api/login",
                    json={"password": VALID_PASSWORD},
                )
                self.assertEqual(login.status_code, 200, login.text)
                self.assertEqual(
                    login.json()["csrf_token"],
                    client.cookies.get("csrf-token"),
                )
                # A valid signed token that differs from the cookie fails the
                # double-submit comparison.
                other = auth.mint_csrf_token()
                mismatched = client.put(
                    "/api/setup/profile",
                    json={"profile": {"llm": {"models": {"default": "x"}}}},
                    headers={
                        "Origin": "https://testserver",
                        "Content-Type": "application/json",
                        "X-CSRF-Token": other,
                    },
                )
                self.assertEqual(mismatched.status_code, 403)


def yaml_load(path):
    import yaml

    return yaml.safe_load(Path(path).read_text())


if __name__ == "__main__":
    unittest.main()
