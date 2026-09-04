"""execute(): dispatch, parameter handling, and failure containment.

execute() runs on the device reader thread for every keypress, so its contract
is strict: the right helper is called with the right arguments, deck-side
actions are delegated to the context, and nothing ever raises out of it — an
exception here would kill the reader thread and silently deaden the deck.
"""
from __future__ import annotations

import sys
import time

import pytest

from fifine_deck import actions
from fifine_deck.model import Action


class Ctx:
    """An ActionContext that records what the engine asked the deck to do."""

    def __init__(self):
        self.calls = []

    def switch_profile(self, profile_id): self.calls.append(("switch_profile", profile_id))
    def next_profile(self): self.calls.append(("next_profile",))
    def prev_profile(self): self.calls.append(("prev_profile",))
    def goto_page(self, index): self.calls.append(("goto_page", index))
    def next_page(self): self.calls.append(("next_page",))
    def prev_page(self): self.calls.append(("prev_page",))
    def show_twitch_chat(self, channel=""): self.calls.append(
        ("show_twitch_chat", channel))
    def set_brightness(self, percent): self.calls.append(("set_brightness", percent))
    def adjust_brightness(self, delta): self.calls.append(("adjust_brightness", delta))
    def sleep_screen(self): self.calls.append(("sleep_screen",))
    def obs_connection(self):
        self.calls.append(("obs_connection",))
        return ("127.0.0.1", 4455, "secret")
    def sound_output(self):
        self.calls.append(("sound_output",))
        return ("", True)


@pytest.fixture
def rec(monkeypatch):
    """Replace every OS-touching helper with a recorder, so tests never spawn
    processes, type keystrokes, or change the machine's volume."""
    calls = []

    def recorder(name):
        def fn(*a, **k):
            calls.append((name, a, k))
        return fn

    for name in ("_popen_detached", "_send_hotkey", "_type_text",
                 "_media", "_volume", "_close_app", "_play_sound",
                 "_chatterino_command", "_power_profile", "_system_power",
                 "_obs_scene", "_obs_transition", "_obs_source",
                 "_obs_output", "_obs_mute"):
        monkeypatch.setattr(actions, name, recorder(name))
    return calls


# -- OS-side actions --------------------------------------------------------

@pytest.mark.parametrize("t", ["launch_app", "run_command"])
def test_launch_and_run_spawn_detached(rec, t):
    actions.execute(Action(t, {"command": "gimp"}))
    assert rec == [("_popen_detached", ("gimp",), {"shell": True})]


@pytest.mark.parametrize("t", ["launch_app", "run_command", "open_url"])
def test_blank_param_is_a_noop(rec, t):
    """An unconfigured key must do nothing rather than spawn an empty shell."""
    actions.execute(Action(t, {"command": "   ", "url": "  "}))
    assert rec == []


def test_open_url_uses_xdg_open(rec):
    actions.execute(Action("open_url", {"url": "https://example.com"}))
    assert rec == [("_popen_detached", (["xdg-open", "https://example.com"],), {})]


def test_hotkey_and_text(rec):
    actions.execute(Action("hotkey", {"keys": "ctrl+shift+m"}))
    actions.execute(Action("text", {"text": "hello"}))
    assert rec == [("_send_hotkey", ("ctrl+shift+m",), {}),
                   ("_type_text", ("hello",), {})]


def test_type_text_press_enter_appends_newline(rec):
    """Chat-style send: optional Enter after the typed text."""
    actions.execute(Action("text", {"text": "hello", "press_enter": "yes"}))
    assert rec == [("_type_text", ("hello\n",), {})]


def test_type_text_press_enter_defaults_off(rec):
    actions.execute(Action("text", {"text": "hello", "press_enter": "no"}))
    actions.execute(Action("text", {"text": "again"}))  # omitted → no Enter
    assert rec == [("_type_text", ("hello",), {}),
                   ("_type_text", ("again",), {})]


def test_media_and_volume_defaults(rec):
    """Params are optional; the documented defaults must be used."""
    actions.execute(Action("media", {}))
    actions.execute(Action("volume", {}))
    assert rec == [("_media", ("play-pause",), {}),
                   ("_volume", ("up", "5"), {})]


def test_close_app(rec):
    actions.execute(Action("close_app", {"target": "firefox"}))
    assert rec == [("_close_app", ("firefox",), {})]


def test_close_app_blank_target_kills_nothing(monkeypatch):
    """Guarded inside _close_app, not execute() — a blank target must never
    reach pkill, which would match every process."""
    ran = []
    monkeypatch.setattr(actions, "_run", lambda *a, **k: ran.append(a))
    actions._close_app("   ")
    assert ran == []


# -- password ---------------------------------------------------------------

def test_password_literal_is_typed(rec):
    actions.execute(Action("password", {"password": "hunter2"}))
    assert rec == [("_type_text", ("hunter2",), {})]


def test_password_resolves_secret_id(rec, monkeypatch):
    from fifine_deck import secret_store
    monkeypatch.setattr(secret_store, "get", lambda sid: "from-keyring" if sid == "s1" else "")
    actions.execute(Action("password", {"secret_id": "s1"}))
    assert rec == [("_type_text", ("from-keyring",), {})]


def test_password_missing_secret_types_nothing_rather_than_none(rec, monkeypatch, caplog):
    """A failed keyring lookup must not type the string 'None' — and must not
    silently type an empty string either. secret_store.get returns None both for
    "no keyring backend" and "keyring is locked", so the key just looked dead and
    the user had no way to tell the secret was merely unavailable."""
    import logging
    from fifine_deck import secret_store
    monkeypatch.setattr(secret_store, "get", lambda sid: None)
    with caplog.at_level(logging.WARNING, logger="fifine_deck.actions"):
        actions.execute(Action("password", {"secret_id": "gone"}))
    assert rec == [], "typing was attempted with no password to type"
    assert any("keyring" in r.message for r in caplog.records), (
        "no explanation logged for a password key that did nothing")


def test_password_with_no_secret_configured_also_explains_itself(rec, caplog):
    """Same dead-key symptom, different cause: nothing was ever stored."""
    import logging
    with caplog.at_level(logging.WARNING, logger="fifine_deck.actions"):
        actions.execute(Action("password", {}))
    assert rec == []
    assert any("no password set" in r.message for r in caplog.records)


def test_typing_without_a_keystroke_tool_says_so(monkeypatch, caplog):
    """0.10.2 audit: _type_text returned silently when no tool was installed,
    while the parallel _send_hotkey path logged. A "Type text" key on a machine
    without xdotool/ydotool/wtype did nothing at all, with no log line and
    nothing on screen — indistinguishable from an unbound key."""
    import logging
    monkeypatch.setattr(actions, "KEY_TOOL", None)
    with caplog.at_level(logging.WARNING, logger="fifine_deck.actions"):
        actions._type_text("hello")
    assert any("no keystroke tool" in r.message for r in caplog.records)


def test_hotkey_canonicalizes_keysyms_for_xdotool(monkeypatch):
    """The app's abbreviations (esc/del/pgup) and symbol forms are not valid X
    keysyms, so on the default xdotool (X11) backend ctrl+esc silently injected
    nothing. They must be canonicalized before xdotool. (maximum-audit)"""
    calls = []
    monkeypatch.setattr(actions, "KEY_TOOL", "xdotool")
    monkeypatch.setattr(actions, "_run", lambda argv, **k: calls.append(argv))
    for combo in ("ctrl+esc", "super+pgup", "ctrl+shift+del", "alt+.", "ctrl+`",
                  "alt+tab", "ctrl+f5", "ctrl+escape", "shift_r+a", "ctrl+f24"):
        actions._send_hotkey(combo)
    sent = [argv[3] for argv in calls]      # ["xdotool","key","--clearmodifiers",combo]
    assert sent == ["ctrl+Escape", "super+Prior", "ctrl+shift+Delete",
                    "alt+period", "ctrl+grave",
                    # round-6 audit: X lookup is case-sensitive, so these
                    # lowercase tokens silently injected nothing on X11
                    "alt+Tab", "ctrl+F5", "ctrl+Escape", "Shift_R+a", "ctrl+F24"]
    # a plain combo passes through untouched
    calls.clear(); actions._send_hotkey("ctrl+c")
    assert calls[0][3] == "ctrl+c"


def test_hotkey_ydotool_covers_punctuation_and_high_f_keys():
    """The reverse gap: keys that worked on xdotool were silently dropped on
    ydotool for want of a keycode. (maximum-audit)"""
    for k in ("grave", "bracketright", "backslash", "apostrophe", "f13", "f24"):
        assert actions._ydotool_keycodes("ctrl+" + k) is not None, \
            f"{k} still missing from _KEYCODES"


def test_brightness_and_goto_page_accept_comma_and_guard_bad_values():
    """fr_FR users type a comma decimal, and these two had no local guard, so a
    comma or any non-number made the key a silent no-op (the value escaped to
    the outer catch-all and the context call never ran). (never-checked-audit)"""
    ctx = Ctx()
    actions.execute(Action("brightness", {"mode": "set", "value": "50"}), ctx)
    actions.execute(Action("brightness", {"mode": "set", "value": "0,5"}), ctx)   # comma
    actions.execute(Action("brightness", {"mode": "set", "value": "oops"}), ctx)  # bad
    actions.execute(Action("goto_page", {"page": "3,0"}), ctx)                    # comma
    actions.execute(Action("goto_page", {"page": "nope"}), ctx)                   # bad
    assert ("set_brightness", 50) in ctx.calls
    assert ("set_brightness", 0) in ctx.calls           # "0,5" parsed, not dropped
    assert ("set_brightness", 10) in ctx.calls          # bad value -> default, NOT a no-op
    assert ("goto_page", 2) in ctx.calls                # "3,0" -> page 3 -> 0-based 2
    assert ("goto_page", 0) in ctx.calls                # bad -> page 1 -> 0-based 0
    # infinities: int(float("1e999")) raises OverflowError, which the first
    # guard missed — the action was dropped instead of falling back (wide-audit)
    ctx2 = Ctx()
    actions.execute(Action("brightness", {"mode": "set", "value": "1e999"}), ctx2)
    actions.execute(Action("goto_page", {"page": "1e999"}), ctx2)
    assert ("set_brightness", 10) in ctx2.calls
    assert ("goto_page", 0) in ctx2.calls


# -- deck-side actions are delegated to the context --------------------------

@pytest.mark.parametrize("t", ["next_page", "prev_page", "next_profile",
                               "prev_profile", "sleep_screen"])
def test_simple_context_actions(t):
    ctx = Ctx()
    actions.execute(Action(t, {}), ctx)
    assert ctx.calls == [(t,)]


def test_switch_profile_passes_id():
    ctx = Ctx()
    actions.execute(Action("switch_profile", {"profile_id": "work"}), ctx)
    assert ctx.calls == [("switch_profile", "work")]


def test_goto_page_converts_to_zero_based():
    """The GUI shows 1-based page numbers; the controller wants 0-based."""
    ctx = Ctx()
    actions.execute(Action("goto_page", {"page": "3"}), ctx)
    assert ctx.calls == [("goto_page", 2)]


def test_show_twitch_chat_dispatches_to_context():
    ctx = Ctx()
    actions.execute(Action("show_twitch_chat", {"channel": "Shroud"}), ctx)
    actions.execute(Action("show_twitch_chat", {}), ctx)
    assert ctx.calls == [
        ("show_twitch_chat", "Shroud"),
        ("show_twitch_chat", ""),
    ]


def test_twitch_snooze_ad_dispatches(monkeypatch):
    seen = {"n": 0}

    def fake():
        seen["n"] += 1

    monkeypatch.setattr(actions, "_twitch_snooze_ad", fake)
    actions.execute(Action("twitch_snooze_ad", {}))
    assert seen["n"] == 1


def test_brightness_modes():
    ctx = Ctx()
    actions.execute(Action("brightness", {"mode": "set", "value": "40"}), ctx)
    actions.execute(Action("brightness", {"mode": "up", "value": "10"}), ctx)
    actions.execute(Action("brightness", {"mode": "down", "value": "10"}), ctx)
    assert ctx.calls == [("set_brightness", 40),
                         ("adjust_brightness", 10),
                         ("adjust_brightness", -10)]


def test_power_profile_dispatches(rec):
    actions.execute(Action("power_profile", {"profile": "performance"}))
    actions.execute(Action("power_profile", {"profile": "cycle"}))
    assert ("_power_profile", ("performance",), {}) in rec
    assert ("_power_profile", ("cycle",), {}) in rec


def test_system_power_dispatches(rec):
    actions.execute(Action("system_power", {"cmd": "sleep"}))
    actions.execute(Action("system_power", {"cmd": "hibernate"}))
    actions.execute(Action("system_power", {"cmd": "shutdown"}))
    assert ("_system_power", ("sleep",), {}) in rec
    assert ("_system_power", ("hibernate",), {}) in rec
    assert ("_system_power", ("shutdown",), {}) in rec


def test_system_power_prefers_loginctl(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return Result()

    monkeypatch.setattr(actions, "_has",
                        lambda cmd: cmd in ("loginctl", "systemctl"))
    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    actions._system_power("sleep")
    actions._system_power("hibernate")
    actions._system_power("shutdown")
    assert calls == [
        ["loginctl", "suspend"],
        ["loginctl", "hibernate"],
        ["loginctl", "poweroff"],
    ]


def test_power_profile_set_and_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "_power_profiles_available",
                        lambda: ["power-saver", "balanced", "performance"])
    monkeypatch.setattr(actions, "_power_profile_get", lambda: "balanced")
    monkeypatch.setattr(
        actions, "_power_profile_set",
        lambda p: calls.append(p) or True)
    actions._power_profile("performance")
    actions._power_profile("cycle")
    actions._power_profile("eco")
    assert calls == ["performance", "performance", "power-saver"]


def test_brightness_direction_wins_over_sign():
    """'down' with an accidentally-negative step must still go down, not up."""
    ctx = Ctx()
    actions.execute(Action("brightness", {"mode": "down", "value": "-10"}), ctx)
    actions.execute(Action("brightness", {"mode": "up", "value": "-10"}), ctx)
    assert ctx.calls == [("adjust_brightness", -10), ("adjust_brightness", 10)]


def test_deck_action_without_context_is_ignored(rec):
    """Deck actions need a context; without one they must log, not raise."""
    actions.execute(Action("next_page", {}), None)
    assert rec == []


# -- multi ------------------------------------------------------------------

def test_multi_runs_steps_in_order(rec):
    actions.execute(Action("multi", {"steps": [
        {"action": {"type": "text", "params": {"text": "one"}}},
        {"action": {"type": "hotkey", "params": {"keys": "ctrl+s"}}},
    ]}))
    assert rec == [("_type_text", ("one",), {}),
                   ("_send_hotkey", ("ctrl+s",), {})]


def test_multi_sleeps_between_steps(rec, monkeypatch):
    slept = []
    monkeypatch.setattr(actions.time, "sleep", slept.append)
    actions.execute(Action("multi", {"steps": [
        {"action": {"type": "text", "params": {"text": "a"}}, "delay": 0.25},
        {"action": {"type": "text", "params": {"text": "b"}}, "delay": 0},
    ]}))
    assert slept == [0.25]          # a zero delay must not sleep at all


def test_multi_step_accepts_bare_action_dict(rec):
    """Steps may be {'action': {...}} or the action dict itself."""
    actions.execute(Action("multi", {"steps": [
        {"type": "text", "params": {"text": "bare"}},
    ]}))
    assert rec == [("_type_text", ("bare",), {})]


def test_multi_reaches_the_context():
    ctx = Ctx()
    actions.execute(Action("multi", {"steps": [
        {"action": {"type": "next_page", "params": {}}},
    ]}), ctx)
    assert ctx.calls == [("next_page",)]


# -- failure containment ----------------------------------------------------

def test_execute_never_raises_when_a_helper_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("helper exploded")
    monkeypatch.setattr(actions, "_type_text", boom)
    actions.execute(Action("text", {"text": "x"}))          # must not raise


def test_execute_never_raises_on_garbage_params():
    ctx = Ctx()
    for action in (Action("goto_page", {"page": "not-a-number"}),
                   Action("brightness", {"mode": "set", "value": "abc"}),
                   Action("multi", {"steps": "not-a-list"}),
                   Action("totally_unknown_type", {})):
        actions.execute(action, ctx)                        # must not raise


def test_multi_bad_step_does_not_abort_remaining_steps(rec, monkeypatch):
    """Audit fix: a malformed step (a non-numeric 'delay', or a non-dict step)
    must not silently drop the remaining steps of a multi-action."""
    steps = [
        {"action": {"type": "run_command", "params": {"command": "one"}}},
        {"action": {"type": "run_command", "params": {"command": "two"}},
         "delay": "0.5s"},                              # bad delay in the middle
        "garbage-not-a-dict",                           # non-dict step
        {"action": {"type": "run_command", "params": {"command": "three"}}},
    ]
    actions.execute(Action("multi", {"steps": steps}))
    ran = [c[1][0] for c in rec if c[0] == "_popen_detached"]
    assert ran == ["one", "two", "three"]


# -- bundle environment hygiene ---------------------------------------------
#
# Everything this app execs is a HOST program: the user's apps and the helpers
# (wpctl, playerctl, xdotool, xdg-open). The AppImage and classic-snap
# launchers export PYTHONHOME/LD_LIBRARY_PATH/QT_PLUGIN_PATH so OUR interpreter
# and Qt resolve inside the bundle, and a child inheriting those breaks: a host
# python3 given our PYTHONHOME dies with "No module named 'encodings'" before
# running a line.

def test_child_env_drops_our_bundle_vars(monkeypatch):
    monkeypatch.setenv("FIFINE_IN_BUNDLE", "1")         # set by AppRun / snap
    monkeypatch.setenv("PYTHONHOME", "/bundle/opt/python3.12")
    monkeypatch.setenv("QT_PLUGIN_PATH", "/bundle/qt/plugins")
    env = actions.child_env()
    assert "PYTHONHOME" not in env
    assert "QT_PLUGIN_PATH" not in env


def test_child_env_restores_the_hosts_own_value(monkeypatch):
    """A stashed FIFINE_HOST_* wins over ours: the child gets what it would
    have had from a terminal, not an empty variable."""
    monkeypatch.setenv("FIFINE_IN_BUNDLE", "1")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/qt/lib:/host/lib")
    monkeypatch.setenv("FIFINE_HOST_LD_LIBRARY_PATH", "/host/lib")
    env = actions.child_env()
    assert env["LD_LIBRARY_PATH"] == "/host/lib"
    # the stash itself must not travel onward, or a nested launch re-applies it
    assert not [k for k in env if k.startswith("FIFINE_HOST_")]


def test_child_env_is_a_passthrough_outside_a_bundle(monkeypatch):
    """.deb, PPA and source installs must be completely unaffected."""
    monkeypatch.delenv("FIFINE_IN_BUNDLE", raising=False)
    monkeypatch.setenv("PYTHONPATH", "/home/user/mylibs")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/whatever/lib")
    env = actions.child_env()
    assert env["PYTHONPATH"] == "/home/user/mylibs"
    assert env["LD_LIBRARY_PATH"] == "/opt/whatever/lib"


def test_launched_programs_get_the_de_bundled_env(monkeypatch, tmp_path):
    """End to end through the real launch path: the child must actually run.

    Without the fix this child never executes a statement — the interpreter
    aborts during startup — so the output file stays empty.
    """
    monkeypatch.setenv("FIFINE_IN_BUNDLE", "1")
    monkeypatch.setenv("PYTHONHOME", "/nonexistent/bundle/python3.12")
    out = tmp_path / "out"
    actions.execute(Action("run_command", {
        "command": f"{sys.executable} -c 'print(\"ran\")' > {out} 2>&1"}))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (out.exists() and out.read_text()):
        time.sleep(0.05)
    assert out.read_text().strip() == "ran"


# -- parameter clamping ------------------------------------------------------

def test_brightness_set_zero_is_not_the_default():
    """`or "10"` turned a JSON numeric 0 into 10, because 0 is falsy."""
    ctx = Ctx()
    actions.execute(Action("brightness", {"mode": "set", "value": 0}), ctx)
    assert ctx.calls == [("set_brightness", 0)]


def test_brightness_blank_value_still_defaults():
    ctx = Ctx()
    actions.execute(Action("brightness", {"mode": "set", "value": ""}), ctx)
    assert ctx.calls == [("set_brightness", 10)]


def test_multi_delay_is_clamped(monkeypatch):
    """An out-of-range delay must clamp, not raise.

    time.sleep(1e300) raises OverflowError, which escaped to execute()'s outer
    guard and dropped every remaining step of the multi.
    """
    slept, ran = [], []
    monkeypatch.setattr(actions.time, "sleep", slept.append)
    monkeypatch.setattr(actions, "_type_text", lambda t: ran.append(t))
    actions.execute(Action("multi", {"steps": [
        {"action": {"type": "text", "params": {"text": "a"}}, "delay": 1e300},
        {"action": {"type": "text", "params": {"text": "b"}}},
    ]}))
    assert ran == ["a", "b"]                 # the later step still ran
    assert slept == [actions.MAX_STEP_DELAY]


def test_volume_step_cannot_become_an_option(monkeypatch):
    """A negative "Step %" built "-20%+", which wpctl reads as a flag."""
    seen = []
    monkeypatch.setattr(actions, "_run", lambda args, **k: seen.append(args))
    monkeypatch.setattr(actions, "AUDIO", "pipewire")
    actions._volume("up", "-20")
    assert not any(str(a).startswith("-2") for a in seen[0])
    assert "20%+" in seen[0]


# -- child_env must not strip on ordinary installs ---------------------------

def test_child_env_does_not_strip_when_only_appdir_is_set(monkeypatch):
    """0.12.0 regression: child_env keyed "am I bundled?" on APPDIR/SNAP, which
    are generic. A plain .deb/source app whose environment carried APPDIR for
    an unrelated reason stripped PYTHONPATH/LD_LIBRARY_PATH from every launched
    program. Only our own FIFINE_IN_BUNDLE marker may trigger the strip."""
    for marker in ("APPDIR", "SNAP"):
        monkeypatch.delenv("FIFINE_IN_BUNDLE", raising=False)
        monkeypatch.setenv(marker, "/some/unrelated/path")
        monkeypatch.setenv("PYTHONPATH", "/home/user/mylibs")
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/cuda/lib")
        env = actions.child_env()
        assert env["PYTHONPATH"] == "/home/user/mylibs", f"{marker} triggered a strip"
        assert env["LD_LIBRARY_PATH"] == "/opt/cuda/lib"


def test_child_env_strips_only_with_the_bundle_marker(monkeypatch):
    monkeypatch.setenv("FIFINE_IN_BUNDLE", "1")
    monkeypatch.setenv("PYTHONHOME", "/bundle/py")
    monkeypatch.setenv("QT_PLUGIN_PATH", "/bundle/qt")
    env = actions.child_env()
    assert "PYTHONHOME" not in env
    assert "QT_PLUGIN_PATH" not in env
    assert "FIFINE_IN_BUNDLE" not in env, "the marker must not reach the child"


def test_child_env_restores_host_values_even_without_the_marker(monkeypatch):
    """An older bundle (0.12.0/0.12.1) has no marker but does write the
    FIFINE_HOST_* stashes; the host's real value is still restored."""
    monkeypatch.delenv("FIFINE_IN_BUNDLE", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/qt:/host/lib")
    monkeypatch.setenv("FIFINE_HOST_LD_LIBRARY_PATH", "/host/lib")
    env = actions.child_env()
    assert env["LD_LIBRARY_PATH"] == "/host/lib"


def test_volume_step_zero_is_a_no_op(monkeypatch):
    """0.12.0 regression: abs()+max(1,..) raised a step of "0" to 1, so a
    zero-step volume key nudged 1% instead of doing nothing."""
    seen = []
    monkeypatch.setattr(actions, "_run", lambda args, **k: seen.append(args))
    monkeypatch.setattr(actions, "AUDIO", "pipewire")
    actions._volume("up", "0")
    assert any("0%+" in str(a) for a in seen[0]), f"step 0 was not a no-op: {seen[0]}"


def test_wtype_maps_right_hand_modifiers_and_super(monkeypatch):
    """wtype rejects an unknown modifier name and exits before emitting
    anything, so the WHOLE combo was silently dropped for ctrl_r/shift_r/alt_r
    — tokens the app blesses and the other two backends handle. "super" is an
    xdotool alias too, not an xkbcommon keysym. (round-7 audit)"""
    calls = []
    monkeypatch.setattr(actions, "KEY_TOOL", "wtype")
    monkeypatch.setattr(actions, "_run", lambda argv, **k: calls.append(argv))

    actions._send_hotkey("shift_r+a")
    assert calls[-1] == ["wtype", "-M", "shift", "-k", "a", "-m", "shift"]

    actions._send_hotkey("ctrl_r+c")
    assert calls[-1] == ["wtype", "-M", "ctrl", "-k", "c", "-m", "ctrl"]

    actions._send_hotkey("win")
    assert calls[-1] == ["wtype", "-k", "Super_L"]

    # a plain combo is unchanged
    actions._send_hotkey("ctrl+shift+m")
    assert calls[-1] == ["wtype", "-M", "ctrl", "-M", "shift", "-k", "m",
                         "-m", "ctrl", "-m", "shift"]


def test_wtype_resolves_a_trailing_bare_modifier(monkeypatch):
    """"alt+shift" (the layout toggle) and a lone "ctrl" end on a modifier
    ALIAS, which is an xdotool spelling, not an xkbcommon keysym name — wtype
    exited before emitting anything and the combo vanished. (round-8 audit)"""
    calls = []
    monkeypatch.setattr(actions, "KEY_TOOL", "wtype")
    monkeypatch.setattr(actions, "_run", lambda argv, **k: calls.append(argv))
    actions._send_hotkey("alt+shift")
    assert calls[-1] == ["wtype", "-M", "alt", "-k", "Shift_L", "-m", "alt"]
    actions._send_hotkey("ctrl")
    assert calls[-1] == ["wtype", "-k", "Control_L"]
    actions._send_hotkey("Super")            # capitals skipped the old guard
    assert calls[-1] == ["wtype", "-k", "Super_L"]


def test_type_text_timeout_scales_with_the_payload(monkeypatch):
    """_run caps every child at 8 s, and typing runs ~40 ms/char on ydotool, so
    a long snippet was killed mid-way (~200 chars) leaving half a line in the
    user's document with only a log line. (round-8 audit)"""
    seen = {}
    monkeypatch.setattr(actions, "KEY_TOOL", "ydotool")
    monkeypatch.setattr(actions, "_run",
                        lambda argv, **k: seen.update(k))
    actions._type_text("x" * 1000)
    assert seen.get("timeout", 8) > 8, "a 1000-char payload still uses the 8 s cap"
    actions._type_text("short")
    assert seen.get("timeout") >= 8, "short text must keep at least the default"


def test_default_icon_survives_a_non_scalar_param():
    """Params are copied verbatim from JSON, so a list/dict here is unhashable
    and dict.get(raw) raised TypeError out of the drag-and-drop slot — dropping
    an action onto such a key did nothing at all. (round-13 audit)"""
    from fifine_deck.actions import default_icon_for
    for t, params in (("volume", {"cmd": ["up"]}),
                      ("media", {"cmd": {"a": 1}}),
                      ("brightness", {"mode": 5})):
        icon, label = default_icon_for(Action(t, params))
        assert icon and label                         # fell back, did not raise
    # the normal lookup still works
    assert default_icon_for(Action("volume", {"cmd": "mute"}))[0] == "mute"


# -- OBS ------------------------------------------------------------------

def test_obs_actions_dispatch(rec):
    ctx = Ctx()
    actions.execute(Action("obs_scene", {"scene": "Cam"}), ctx)
    actions.execute(Action("obs_preview_scene", {"scene": "BRB"}), ctx)
    actions.execute(Action("obs_transition", {}), ctx)
    actions.execute(Action("obs_source",
                           {"scene": "Main", "source": "Webcam",
                            "state": "hide"}), ctx)
    actions.execute(Action("obs_recording", {"cmd": "toggle"}), ctx)
    actions.execute(Action("obs_streaming", {"cmd": "start"}), ctx)
    actions.execute(Action("obs_mute",
                           {"input": "Mic/Aux", "state": "mute"}), ctx)
    assert rec == [
        ("_obs_scene", (ctx, "Cam"), {}),
        ("_obs_scene", (ctx, "BRB"), {"preview": True}),
        ("_obs_transition", (ctx,), {}),
        ("_obs_source", (ctx, "Main", "Webcam", "hide"), {}),
        ("_obs_output", (ctx, "recording", "toggle"), {}),
        ("_obs_output", (ctx, "streaming", "start"), {}),
        ("_obs_mute", (ctx, "Mic/Aux", "mute"), {}),
    ]


def test_obs_request_maps_to_websocket(monkeypatch):
    calls = []

    def fake_call(host, port, password, req, data=None):
        calls.append((host, port, password, req, data))
        if req == "GetSceneList":
            return {"scenes": [{"sceneName": "Intro"},
                               {"sceneName": "BRB"}]}
        if req == "GetRecordStatus":
            return {"outputActive": True}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    ctx = Ctx()
    actions._obs_scene(ctx, "Intro")
    actions._obs_output(ctx, "recording", "stop")
    assert ("127.0.0.1", 4455, "secret", "GetSceneList", None) in calls
    assert ("127.0.0.1", 4455, "secret", "SetCurrentProgramScene",
            {"sceneName": "Intro"}) in calls
    assert ("127.0.0.1", 4455, "secret", "GetRecordStatus", None) in calls
    assert ("127.0.0.1", 4455, "secret", "StopRecord", None) in calls


def test_obs_scene_resolves_aliases_case_insensitively(monkeypatch):
    calls = []

    def fake_call(host, port, password, req, data=None):
        calls.append((req, data))
        if req == "GetSceneList":
            return {"scenes": [
                {"sceneName": "Be Right Back"},
                {"sceneName": "Starting Soon!"},
                {"sceneName": "Game Capture"},
            ]}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    actions._obs_scene(Ctx(), "BRB")
    actions._obs_scene(Ctx(), "starting soon")
    actions._obs_scene(Ctx(), "game")
    sets = [c for c in calls if c[0] == "SetCurrentProgramScene"]
    assert sets[0][1]["sceneName"] == "Be Right Back"
    assert sets[1][1]["sceneName"] == "Starting Soon!"
    assert sets[2][1]["sceneName"] == "Game Capture"


def test_obs_start_skips_when_already_active(monkeypatch):
    calls = []

    def fake_call(host, port, password, req, data=None):
        calls.append(req)
        if req == "GetStreamStatus":
            return {"outputActive": True}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    actions._obs_output(Ctx(), "streaming", "start")
    assert calls == ["GetStreamStatus"]
    assert "StartStream" not in calls


def test_obs_mute_resolves_mic_input(monkeypatch):
    calls = []

    def fake_call(host, port, password, req, data=None):
        calls.append((req, data))
        if req == "GetInputList":
            return {"inputs": [
                {"inputName": "Desktop Audio"},
                {"inputName": "Mic"},
            ]}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    actions._obs_mute(Ctx(), "Mic/Aux", "toggle")
    assert ("ToggleInputMute", {"inputName": "Mic"}) in calls


def test_obs_blank_scene_is_noop(monkeypatch, caplog):
    import logging
    calls = []
    monkeypatch.setattr(
        "fifine_deck.obs_ws.call",
        lambda *a, **k: calls.append(a) or {})
    with caplog.at_level(logging.WARNING, logger="fifine_deck.actions"):
        actions._obs_scene(Ctx(), "   ")
    assert calls == []
    assert any("no scene name" in r.message for r in caplog.records)


def test_obs_source_toggle_fetches_id_and_state(monkeypatch):
    calls = []

    def fake_call(host, port, password, req, data=None):
        calls.append((req, data))
        if req == "GetSceneList":
            return {"scenes": [{"sceneName": "Main"}]}
        if req == "GetSceneItemList":
            return {"sceneItems": [{"sourceName": "Cam"}]}
        if req == "GetSceneItemId":
            return {"sceneItemId": 7}
        if req == "GetSceneItemEnabled":
            return {"sceneItemEnabled": True}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    actions._obs_source(Ctx(), "Main", "Cam", "toggle")
    assert calls == [
        ("GetSceneList", None),
        ("GetSceneItemList", {"sceneName": "Main"}),
        ("GetSceneItemId", {"sceneName": "Main", "sourceName": "Cam"}),
        ("GetSceneItemEnabled", {"sceneName": "Main", "sceneItemId": 7}),
        ("SetSceneItemEnabled",
         {"sceneName": "Main", "sceneItemId": 7, "sceneItemEnabled": False}),
    ]


def test_obs_source_resolves_brb_alias(monkeypatch):
    calls = []

    def fake_call(host, port, password, req, data=None):
        calls.append((req, data))
        if req == "GetSceneList":
            return {"scenes": [{"sceneName": "Live"},
                               {"sceneName": "Be Right Back"}]}
        if req == "GetSceneItemList":
            scene = (data or {}).get("sceneName")
            if scene == "Live":
                return {"sceneItems": [{"sourceName": "Webcam"},
                                       {"sourceName": "Be Right Back"}]}
            return {"sceneItems": []}
        if req == "GetSceneItemId":
            return {"sceneItemId": 3}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    actions._obs_source(Ctx(), "Live", "BRB", "show")
    assert ("GetSceneItemId",
            {"sceneName": "Live", "sourceName": "Be Right Back"}) in calls
    assert ("SetSceneItemEnabled",
            {"sceneName": "Live", "sceneItemId": 3,
             "sceneItemEnabled": True}) in calls


def test_obs_source_scans_other_scenes_for_brb(monkeypatch):
    calls = []

    def fake_call(host, port, password, req, data=None):
        calls.append((req, data))
        if req == "GetSceneList":
            return {"scenes": [{"sceneName": "Live"},
                               {"sceneName": "Main"}]}
        if req == "GetSceneItemList":
            scene = (data or {}).get("sceneName")
            if scene == "Live":
                return {"sceneItems": [{"sourceName": "Webcam"}]}
            if scene == "Main":
                return {"sceneItems": [{"sourceName": "BRB Overlay"}]}
            return {"sceneItems": []}
        if req == "GetSceneItemId":
            return {"sceneItemId": 9}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    actions._obs_source(Ctx(), "Live", "BRB", "hide")
    assert ("GetSceneItemId",
            {"sceneName": "Main", "sourceName": "BRB Overlay"}) in calls


def test_obs_mute_prefers_special_mic(monkeypatch):
    calls = []

    def fake_call(host, port, password, req, data=None):
        calls.append((req, data))
        if req == "GetSpecialInputs":
            return {"mic1": "Fifine USB Mic", "desktop1": "Desktop Audio"}
        if req == "GetInputList":
            return {"inputs": [
                {"inputName": "Desktop Audio", "inputKind": "pulse_output_capture"},
                {"inputName": "Fifine USB Mic", "inputKind": "pulse_input_capture"},
            ]}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    actions._obs_mute(Ctx(), "Mic/Aux", "toggle")
    assert ("ToggleInputMute", {"inputName": "Fifine USB Mic"}) in calls


def test_autofill_obs_params_rewrites_chip_names(monkeypatch):
    def fake_call(host, port, password, req, data=None):
        if req == "GetSceneList":
            return {"scenes": [{"sceneName": "Be Right Back"},
                               {"sceneName": "Main Live"}],
                    "currentProgramSceneName": "Main Live"}
        if req == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "Main Live"}
        if req == "GetSpecialInputs":
            return {"mic1": "USB Mic"}
        if req == "GetInputList":
            return {"inputs": [
                {"inputName": "USB Mic", "inputKind": "pulse_input_capture"},
            ]}
        if req == "GetSceneItemList":
            return {"sceneItems": [{"sourceName": "BRB Screen"}]}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    scene = actions.autofill_obs_params(
        "obs_scene", {"scene": "BRB"}, "127.0.0.1", 4455, "")
    assert scene["scene"] == "Be Right Back"
    mute = actions.autofill_obs_params(
        "obs_mute", {"input": "Mic/Aux", "state": "toggle"},
        "127.0.0.1", 4455, "")
    assert mute["input"] == "USB Mic"
    src = actions.autofill_obs_params(
        "obs_source",
        {"scene": "Live", "source": "BRB", "state": "toggle"},
        "127.0.0.1", 4455, "")
    assert src["scene"] == "Main Live"
    assert src["source"] == "BRB Screen"


def test_resolve_obs_editor_field_and_current_scene(monkeypatch):
    def fake_call(host, port, password, req, data=None):
        if req == "GetSceneList":
            return {"scenes": [{"sceneName": "Cam"},
                               {"sceneName": "Main Live"}],
                    "currentProgramSceneName": "Main Live"}
        if req == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "Main Live"}
        if req == "GetSpecialInputs":
            return {"mic1": "Fifine"}
        if req == "GetInputList":
            return {"inputs": [
                {"inputName": "Fifine", "inputKind": "pulse_input_capture"},
                {"inputName": "Desktop", "inputKind": "pulse_output_capture"},
            ]}
        if req == "GetSceneItemList":
            return {"sceneItems": [{"sourceName": "Webcam"},
                                   {"sourceName": "BRB Overlay"}]}
        return {}

    monkeypatch.setattr("fifine_deck.obs_ws.call", fake_call)
    monkeypatch.setattr("fifine_deck.obs_ws.ensure", lambda *a, **k: True)
    assert actions.list_obs_current_scene("127.0.0.1", 4455, "") == "Main Live"
    scenes = actions.list_obs_scenes("127.0.0.1", 4455, "")
    assert scenes[0] == "Main Live"
    assert actions.resolve_obs_editor_field(
        "obs_input", "Mic/Aux", "127.0.0.1", 4455, "") == "Fifine"
    assert actions.resolve_obs_editor_field(
        "obs_input", "", "127.0.0.1", 4455, "") == "Fifine"
    assert actions.resolve_obs_editor_field(
        "obs_source", "BRB", "127.0.0.1", 4455, "",
        scene="") == "BRB Overlay"
    sources = actions.list_obs_scene_sources("127.0.0.1", 4455, "", "")
    assert "Webcam" in sources
    ok, msg = actions.obs_connection_summary("127.0.0.1", 4455, "")
    assert ok and "connected" in msg.lower() and "Main Live" in msg


def test_play_sound_dispatches(rec):
    actions.execute(Action("play_sound", {
        "clip": "bruh", "file": "", "volume": "70"}))
    assert len(rec) == 1
    assert rec[0][0] == "_play_sound"
    assert rec[0][1][:3] == ("bruh", "", "70")


def test_play_sound_uses_context_sink(monkeypatch):
    seen = {}

    class SCtx(Ctx):
        def sound_output(self):
            return ("easyeffects_sink", True)

    def fake_play(clip, file_path="", volume=80, sink="", also_default=True):
        seen.update(clip=clip, sink=sink, also_default=also_default,
                    volume=volume)
        return True

    monkeypatch.setattr("fifine_deck.sounds.play", fake_play)
    actions._play_sound("bruh", "", "80", SCtx())
    assert seen["sink"] == "easyeffects_sink"
    assert seen["also_default"] is True


def test_chatterino_clip_dispatches(rec):
    actions.execute(Action("chatterino", {
        "command": "/clip", "window": "chatterino"}))
    assert rec == [("_chatterino_command", ("/clip", "chatterino"), {})]


def test_chatterino_command_focuses_and_types(monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "KEY_TOOL", "xdotool")
    monkeypatch.setattr(actions, "_focus_app_window",
                        lambda hint: calls.append(("focus", hint)) or True)
    monkeypatch.setattr(actions, "_type_text",
                        lambda text: calls.append(("type", text)))
    monkeypatch.setattr(actions.time, "sleep", lambda *_: None)
    actions._chatterino_command("/clip", "chatterino")
    assert calls == [("focus", "chatterino"), ("type", "/clip\n")]


def test_chatterino_command_adds_slash_and_skips_without_window(monkeypatch, caplog):
    import logging
    typed = []
    monkeypatch.setattr(actions, "KEY_TOOL", "xdotool")
    monkeypatch.setattr(actions, "_focus_app_window", lambda hint: False)
    monkeypatch.setattr(actions, "_type_text",
                        lambda text: typed.append(text))
    with caplog.at_level(logging.WARNING, logger="fifine_deck.actions"):
        actions._chatterino_command("clip", "chatterino")
    assert typed == []
    assert any("not found" in r.message for r in caplog.records)


def test_obs_helpers_never_raise_when_ws_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("fifine_deck.obs_ws.call", boom)
    # execute()'s outer guard must keep a broken OBS path from killing the
    # reader thread — even if a helper lets an unexpected exception through.
    actions.execute(Action("obs_recording", {"cmd": "toggle"}), Ctx())
