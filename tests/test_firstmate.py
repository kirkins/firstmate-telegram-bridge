"""Unit battery for firstmate.py against recorded state-file shapes.

All fixtures are synthetic: a temp "firstmate home" holding state/*.meta and
*.status files shaped like the real ones, plus a stubbed `herdr agent list`.
"""

import os
import tempfile
import unittest

import _env  # noqa: F401  (env must be set before tg imports)
import firstmate
import tg
from _env import FOREIGN, MAIN, SCOUT, WORKER, FakeHerdr, agent, make_home

TASK = ("bridge-fork-build", WORKER, "ship",
        ["working [at=1790000000]: started", "done [at=1790000100]: PR opened"])
SCOUT_TASK = ("scout-thing", SCOUT, "scout", ["blocked [at=1790000005]: waiting"])


def home_cfg(home, **fm):
    cfg = {"firstmate": {"enabled": True, "main_cwd": home}, "allowed_user_ids": [1]}
    cfg["firstmate"].update(fm)
    return cfg


def fleet(home, main_status="working"):
    """A pane population with one main, one claimed worker, one claimed scout,
    one foreign pane — the live shape, synthetic ids."""
    return [agent(MAIN, home, status=main_status),
            agent(WORKER, f"{home}-treehouse/worker1", cli="opencode",
                  status="done", title="OC | build the fork"),
            agent(SCOUT, f"{home}-treehouse/scout1", status="idle"),
            agent(FOREIGN, "/srv/unrelated/project", status="idle")]


class TestConfig(unittest.TestCase):
    def test_section_defaults_on_but_absent_section_stays_generic(self):
        self.assertFalse(firstmate.enabled({}))
        self.assertFalse(firstmate.enabled({"firstmate": "yes"}))
        self.assertTrue(firstmate.enabled({"firstmate": {}}))
        self.assertFalse(firstmate.enabled({"firstmate": {"enabled": False}}))
        self.assertTrue(firstmate.enabled({"firstmate": {"enabled": True}}))

    def test_defaults(self):
        cfg = home_cfg("/x")
        self.assertEqual(firstmate.main_cwd(cfg), "/x")
        self.assertEqual(firstmate.main_cwd({"firstmate": {"enabled": True}}),
                         os.path.expanduser("~/firstmate"))
        self.assertFalse(firstmate.allow_worker_esc(cfg))

    def test_replies_allowed_requires_allowlist_when_enabled(self):
        self.assertTrue(firstmate.replies_allowed({}))
        self.assertTrue(firstmate.replies_allowed({"allowed_user_ids": []}))
        cfg = home_cfg("/x")
        cfg["allowed_user_ids"] = []
        self.assertFalse(firstmate.replies_allowed(cfg))       # empty -> degrade
        cfg["allowed_user_ids"] = [123456789]
        self.assertTrue(firstmate.replies_allowed(cfg))

    def test_mode_check_warnings(self):
        self.assertEqual(firstmate.mode_check({}), [])
        cfg = home_cfg("/x")
        cfg["allowed_user_ids"] = []
        self.assertEqual(len(firstmate.mode_check(cfg)), 1)       # allowlist empty
        cfg["allowed_user_ids"] = [1]
        self.assertEqual(firstmate.mode_check(cfg), [])
        cfg["firstmate"]["worker_notify"] = True
        warns = firstmate.mode_check(cfg)                          # inert key named
        self.assertEqual(len(warns), 1)
        self.assertIn("worker_notify", warns[0])
        self.assertIn("main-pane only", warns[0])


class TestMainPane(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="fm-unit-")
        self.home = make_home(self.base, tasks=[TASK, SCOUT_TASK])
        self.cfg = home_cfg(self.home)

    def herdr(self, agents):
        fake = FakeHerdr(agents)
        tg.herdr_json = fake.json
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        return fake

    def test_none_when_disabled(self):
        self.herdr(fleet(self.home))
        self.assertIsNone(firstmate.main_pane({"firstmate": {"enabled": False}}))

    def test_single_cwd_match(self):
        self.herdr(fleet(self.home))
        found = firstmate.main_pane(self.cfg)
        self.assertEqual(found["pane"], MAIN)
        self.assertEqual(found["agent"], "pi")
        self.assertTrue(found["folder"].endswith("home"))

    def test_no_match_fails_closed(self):
        self.herdr([agent(WORKER, "/elsewhere")])
        self.assertIsNone(firstmate.main_pane(self.cfg))

    def test_two_matches_impostor_claimed_prefers_unclaimed_main(self):
        # both panes sit in the home cwd; a meta claims the impostor only — the
        # unclaimed candidate is the main pane and must win
        make_home(self.base, tasks=[("other-task", "w5:p9", "ship", ["done [at=1]: x"])])
        impostor = agent("w5:p9", self.home)
        self.herdr([agent(MAIN, self.home), impostor])
        found = firstmate.main_pane(self.cfg)
        self.assertEqual(found["pane"], MAIN)          # unclaimed candidate wins

    def test_two_matches_main_claimed_prefers_unclaimed_impostor(self):
        # pathological: a meta claims MAIN's pane; then the unclaimed impostor wins
        # — which is exactly why claims are only a tiebreak, never an override
        make_home(self.base, tasks=[("main-claim", MAIN, "ship", [])])
        impostor = agent("w5:p9", self.home)
        self.herdr([agent(MAIN, self.home), impostor])
        self.assertEqual(firstmate.main_pane(self.cfg)["pane"], "w5:p9")

    def test_two_matches_both_unclaimed_is_ambiguous(self):
        self.herdr([agent(MAIN, self.home), agent("w5:p9", self.home)])
        self.assertIsNone(firstmate.main_pane(self.cfg))   # fail closed

    def test_two_matches_both_claimed_is_ambiguous(self):
        make_home(self.base, tasks=[("a", MAIN, "ship", []), ("b", "w5:p9", "ship", [])])
        self.herdr([agent(MAIN, self.home), agent("w5:p9", self.home)])
        self.assertIsNone(firstmate.main_pane(self.cfg))   # fail closed

    def test_pin_validated_live(self):
        fake = self.herdr(fleet(self.home))
        cfg = home_cfg(self.home, main_pane=WORKER)
        self.assertEqual(firstmate.main_pane(cfg)["pane"], WORKER)
        cfg = home_cfg(self.home, main_pane="wZ:p9")
        fake.agents = fleet(self.home)
        self.assertEqual(firstmate.main_pane(cfg)["pane"], MAIN)  # dead pin -> cwd scan


class TestRelayAllowlist(unittest.TestCase):
    """relay_denied: the outbound allowlist is the resolved main-pane identity —
    exact pane-id equality, never a name match. Workers, scouts, foreign panes
    and an unresolvable main pane all deny; only the main pane relays."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="fm-relay-")
        self.home = make_home(self.base, tasks=[TASK, SCOUT_TASK])
        self.cfg = home_cfg(self.home)

    def herdr(self, agents):
        fake = FakeHerdr(agents)
        tg.herdr_json = fake.json
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        return fake

    def test_everything_relays_when_mode_is_off(self):
        self.herdr(fleet(self.home))
        off = {"firstmate": {"enabled": False, "main_cwd": self.home}}
        for pane in (MAIN, WORKER, SCOUT, FOREIGN):
            self.assertEqual(firstmate.relay_denied(off, pane), "", pane)

    def test_only_the_main_pane_relays(self):
        self.herdr(fleet(self.home))
        self.assertEqual(firstmate.relay_denied(self.cfg, MAIN), "")
        for pane in (WORKER, SCOUT, FOREIGN):
            denied = firstmate.relay_denied(self.cfg, pane)
            self.assertTrue(denied.startswith("firstmate: outbound relay is"), pane)
            self.assertIn(pane, denied)
            self.assertIn(f"main={MAIN}", denied)

    def test_unresolvable_main_denies_everything(self):
        self.herdr([agent(WORKER, "/elsewhere")])
        for pane in (MAIN, WORKER):
            denied = firstmate.relay_denied(self.cfg, pane)
            self.assertIn("main pane unidentifiable", denied)
            self.assertIn("failing closed", denied)

    def test_an_impostor_in_the_home_cwd_is_not_main(self):
        # a worker whose reported cwd IS the firstmate home (herdr reports a
        # spawned pane's cwd as its spawn origin) must not relay: the meta-claimed
        # impostor loses the tiebreak and only the unclaimed main pane passes
        self.herdr([agent(MAIN, self.home), agent(WORKER, self.home)])
        self.assertEqual(firstmate.relay_denied(self.cfg, MAIN), "")
        self.assertTrue(firstmate.relay_denied(self.cfg, WORKER))


class TestTaskContext(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="fm-unit-")
        self.home = make_home(self.base, tasks=[TASK, SCOUT_TASK])
        self.cfg = home_cfg(self.home)

    def test_meta_and_status_tail(self):
        ctx = firstmate.task_context(self.cfg, WORKER)
        self.assertEqual(ctx["task"], "bridge-fork-build")
        self.assertEqual(ctx["kind"], "ship")
        self.assertEqual(ctx["project"], "/tmp/fmtest/project")
        self.assertEqual(ctx["status"], "done")      # state word of the last line

    def test_foreign_pane_has_no_context(self):
        self.assertEqual(firstmate.task_context(self.cfg, FOREIGN), {})

    def test_badge_and_context_line(self):
        line = firstmate.context_line(self.cfg, WORKER, "opencode")
        self.assertEqual(line, "[opencode/bridge-fork-build pane w9:p2]")
        self.assertEqual(firstmate.context_line(self.cfg, FOREIGN, "pi"),
                         f"[pi/{FOREIGN} pane {FOREIGN}]")   # no task -> pane id

    def test_status_file_missing_omits_status(self):
        os.remove(os.path.join(self.home, "state", "bridge-fork-build.status"))
        ctx = firstmate.task_context(self.cfg, WORKER)
        self.assertNotIn("status", ctx)


class TestRoutingHelpers(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="fm-unit-")
        self.home = make_home(self.base, tasks=[TASK])
        self.cfg = home_cfg(self.home)
        self.off = {"firstmate": {"enabled": False, "main_cwd": self.home}}
        tg.herdr_json = FakeHerdr(fleet(self.home)).json
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        self.main = firstmate.main_target(self.cfg)
        self.worker = {"pane": WORKER, "agent": "opencode", "folder": "~/w"}

    def test_retarget_passthrough_cases(self):
        self.assertEqual(firstmate.retarget(self.off, self.worker), self.worker)
        self.assertEqual(firstmate.retarget(self.cfg, {}), {})
        kept = {**self.main, "context": "sticky"}
        self.assertEqual(firstmate.retarget(self.cfg, kept), kept)   # main keeps context

    def test_retarget_worker_becomes_main_with_context(self):
        out = firstmate.retarget(self.cfg, self.worker)
        self.assertEqual(out["pane"], MAIN)
        self.assertEqual(out["worker_pane"], WORKER)
        self.assertEqual(out["context"], "[opencode/bridge-fork-build pane w9:p2]")

    def test_resolve_target_fresh_start_is_main(self):
        self.assertEqual(firstmate.resolve_target(self.cfg, {})["pane"], MAIN)

    def test_resolve_target_degrades_without_allowlist(self):
        self.cfg["allowed_user_ids"] = []
        self.assertEqual(firstmate.resolve_target(self.cfg, self.worker), {})
        self.cfg["allowed_user_ids"] = [1]
        self.assertEqual(firstmate.resolve_target(self.cfg, self.worker)["pane"], MAIN)

    def test_resolve_target_fails_closed_without_main(self):
        tg.herdr_json = FakeHerdr([agent(WORKER, "/elsewhere")]).json
        self.assertEqual(firstmate.resolve_target(self.cfg, {}), {})
        self.assertEqual(firstmate.resolve_target(self.cfg, self.worker), {})

    def test_prefixed_only_with_context(self):
        self.assertEqual(firstmate.prefixed(self.cfg, self.worker, "hi"), "hi")
        t = firstmate.retarget(self.cfg, self.worker)
        self.assertEqual(firstmate.prefixed(self.cfg, t, "hi"), f"{t['context']}\nhi")
        self.assertEqual(firstmate.prefixed(self.off, t, "hi"), "hi")

    def test_command_target_defaults_to_main(self):
        self.assertEqual(firstmate.command_target(self.cfg, {})["pane"], MAIN)
        self.assertEqual(firstmate.command_target(self.off, {}), {})
        self.assertEqual(firstmate.command_target(self.cfg, self.worker), self.worker)

    def test_read_pane_prefers_worker(self):
        t = firstmate.retarget(self.cfg, self.worker)
        self.assertEqual(firstmate.read_pane(self.cfg, t), WORKER)
        self.assertEqual(firstmate.read_pane(self.cfg, self.main), MAIN)
        self.assertEqual(firstmate.read_pane(self.off, t), t["pane"])

    def test_esc_target_matrix(self):
        t = firstmate.retarget(self.cfg, self.worker)     # worker notification reply
        self.assertEqual(firstmate.esc_target(self.cfg, t), "")            # refused
        self.assertEqual(firstmate.esc_target(self.cfg, self.main), MAIN)  # main ok
        self.cfg["firstmate"]["allow_worker_esc"] = True
        self.assertEqual(firstmate.esc_target(self.cfg, t), WORKER)        # hatch open
        stale = {"pane": SCOUT, "agent": "pi", "folder": "~"}              # no context
        self.cfg["firstmate"]["allow_worker_esc"] = False
        self.assertEqual(firstmate.esc_target(self.cfg, stale), "")        # fail closed
        self.assertEqual(firstmate.esc_target(self.off, stale), SCOUT)     # upstream


class TestCalm(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="fm-unit-")
        self.home = make_home(self.base, tasks=[TASK, SCOUT_TASK])
        tg.herdr_json = FakeHerdr(fleet(self.home)).json
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)

    def cfg(self, calm):
        if calm is not None:
            os.makedirs(os.path.join(self.home, "config"), exist_ok=True)
            with open(os.path.join(self.home, "config", "calm"), "w",
                      encoding="utf-8") as f:
                f.write(f"{calm}\n")
        return home_cfg(self.home)

    def test_preference_resolution(self):
        for calm, want in (("on", True), ("max", True), ("off", False),
                           ("ON\n", True), ("bogus", False), ("", False)):
            self.assertEqual(firstmate.calm_on(self.cfg(calm)), want, calm)
        self.cfg(None)
        try:
            os.remove(os.path.join(self.home, "config", "calm"))
        except FileNotFoundError:
            pass
        self.assertFalse(firstmate.calm_on(home_cfg(self.home)))   # absent = off

    def short(self):
        return "Let me check that file."

    def test_calm_excerpt_rules(self):
        cfg = self.cfg("on")
        long_line = "x" * firstmate.CALM_PRESERVE_MIN_CHARS
        self.assertEqual(firstmate.calm_excerpt(cfg, WORKER, "done", self.short()),
                         self.short())                          # final reply always kept
        self.assertEqual(firstmate.calm_excerpt(cfg, WORKER, "blocked", self.short()),
                         "")                                   # narration dropped
        self.assertEqual(firstmate.calm_excerpt(cfg, WORKER, "blocked", long_line),
                         long_line)                             # >= 240 trimmed kept
        self.assertEqual(firstmate.calm_excerpt(cfg, WORKER, "blocked", "x" * 239), "")
        self.assertEqual(firstmate.calm_excerpt(cfg, WORKER, "blocked", "one\ntwo"),
                         "one\ntwo")                            # newline kept
        self.assertEqual(firstmate.calm_excerpt(cfg, FOREIGN, "blocked", self.short()),
                         self.short())                          # foreign pane untouched
        self.assertEqual(firstmate.calm_excerpt(self.cfg("off"), WORKER, "blocked",
                                                self.short()), self.short())
        off = {"firstmate": {"enabled": False}}
        self.assertEqual(firstmate.calm_excerpt(off, WORKER, "blocked", self.short()),
                         self.short())                          # mode off untouched

    def test_scout_panes_are_firstmate_panes(self):
        cfg = self.cfg("on")
        self.assertTrue(firstmate.is_firstmate_pane(cfg, SCOUT))
        self.assertFalse(firstmate.is_firstmate_pane(cfg, FOREIGN))


if __name__ == "__main__":
    unittest.main()
