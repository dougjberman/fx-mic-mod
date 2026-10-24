#!/usr/bin/env python3
"""Run the hardware test plan's automatic checks on a Ting / FX Mic over USB.

    pip install pyserial numpy
    python3 tools/ting_test.py check              # everything that needs no hands
    python3 tools/ting_test.py guided             # LEDs, buttons, handle, knob, with prompts
    python3 tools/ting_test.py install            # copy the firmware over USB serial
    python3 tools/ting_test.py backup DEST        # copy the mic's drive to a folder

The mic must be running CircuitPython with fx-mic-mod, powered on normally
(not in edit mode), and plugged in with a data cable. The suite stops
code.py, puts selftest.py and three test WAVs on the drive, runs the checks
from the serial REPL, then restarts code.py. Results go to
test-results/<date-time>/: report.md, every recording the mic made, and
the raw log.

Audio checks are an acoustic loopback: the mic plays a test WAV through a
preset out of its own speaker and records it with its own mic. Run them
somewhere quiet, with headphones unplugged.
"""

import argparse
import base64
import datetime
import glob
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIRMWARE = os.path.join(ROOT, "firmware")
sys.path.insert(0, HERE)

import ting_analysis as A  # noqa: E402

# Files install puts on the drive, code.py last so a half-finished copy
# never starts.
FIRMWARE_FILES = ("config_loader.py", "autotune.py", "recorder.py", "selftest.py",
                  "boot.py", "code.py")
DRIVE_NAMES = ("CIRCUITPY", "TING DISK", "FX-MIC DISK", "fx-mic disk")


# --- Serial REPL ---


class ReplError(Exception):
    pass


class RawRepl:
    """CircuitPython's raw REPL: send code, read its output as it comes."""

    def __init__(self, port, timeout=10.0, link=None):
        if link is None:
            import serial  # pyserial
            link = serial.Serial(port, 115200, timeout=0.1)
        self.serial = link
        self.timeout = timeout
        self.buf = b""

    def _read_until(self, marker, timeout=None, on_line=None):
        """Bytes up to ``marker`` (dropped); anything after stays buffered.
        ``on_line`` gets each complete line as it arrives."""
        wait = timeout or self.timeout
        deadline = time.time() + wait
        done = 0
        while True:
            end = self.buf.find(marker)
            if on_line is not None:
                limit = end if end >= 0 else len(self.buf)
                while True:
                    nl = self.buf.find(b"\n", done, limit)
                    if nl < 0:
                        break
                    on_line(self.buf[done:nl].decode("utf-8", "replace").rstrip("\r"))
                    done = nl + 1
            if end >= 0:
                if on_line is not None and done < end:
                    on_line(self.buf[done:end].decode("utf-8", "replace").rstrip("\r"))
                out, self.buf = self.buf[:end], self.buf[end + len(marker):]
                return out
            chunk = self.serial.read(4096)
            if chunk:
                self.buf += chunk
                deadline = time.time() + wait
            elif time.time() > deadline:
                raise ReplError("timed out waiting for {!r}; got {!r}".format(marker, self.buf[-200:]))

    def enter(self):
        """Stop code.py and switch to the raw REPL."""
        s = self.serial
        s.write(b"\r\x03\x03")
        time.sleep(0.3)
        s.write(b"\r")
        time.sleep(0.3)
        s.reset_input_buffer()
        self.buf = b""
        s.write(b"\x01")
        self._read_until(b"raw REPL; CTRL-B to exit\r\n>")

    def exec(self, code, timeout=None, on_line=None):
        """Run ``code``; return its stdout. A device exception raises."""
        s = self.serial
        data = code.encode()
        for i in range(0, len(data), 256):
            s.write(data[i:i + 256])
            time.sleep(0.005)
        s.write(b"\x04")
        self._read_until(b"OK", timeout)
        out = self._read_until(b"\x04", timeout, on_line)
        err = self._read_until(b"\x04", timeout).decode("utf-8", "replace").strip()
        self._read_until(b">", timeout)
        if err:
            raise ReplError(err)
        return out.decode("utf-8", "replace")

    def leave(self, restart=True):
        """Back to the normal REPL, and restart code.py."""
        self.serial.write(b"\x02")
        if restart:
            time.sleep(0.2)
            self.serial.write(b"\x04")
        time.sleep(0.3)
        self.serial.close()


def find_port():
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    for p in ports:
        text = " ".join(str(v) for v in (p.description, p.manufacturer, p.product) if v)
        if p.vid == 0x239A or "CircuitPython" in text:
            return p.device
    names = ", ".join(p.device for p in ports) or "none"
    raise SystemExit("No CircuitPython serial port found (ports: {}). Pass --port.".format(names))


def find_drive():
    roots = ["/Volumes", "/media/" + os.environ.get("USER", ""), "/run/media/" + os.environ.get("USER", "")]
    for root in roots:
        for name in DRIVE_NAMES:
            path = os.path.join(root, name)
            if os.path.isdir(path):
                return path
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            path = letter + ":\\"
            if os.path.exists(os.path.join(path, "boot_out.txt")):
                return path
    return None


# --- Device calls ---


class Device:
    """Runs selftest functions and collects their "@@" records."""

    def __init__(self, repl, log):
        self.repl = repl
        self.log = log

    def call(self, code, timeout=60.0):
        records = []
        wav = {"name": None, "chunks": []}
        waves = {}

        def on_line(line):
            self.log.write(line + "\n")
            if line.startswith("@@B"):
                wav["chunks"].append(line[3:])
            elif line.startswith("@@"):
                rec = json.loads(line[2:])
                if rec["t"] == "wav_begin":
                    wav["name"], wav["chunks"] = rec["name"], []
                elif rec["t"] == "wav_end":
                    raw = b"".join(base64.b64decode(c) for c in wav["chunks"])
                    waves[rec["name"]] = A.np.frombuffer(raw, dtype="<i2")
                else:
                    records.append(rec)
            elif line.strip():
                print("   mic: " + line)

        self.log.write(">>> " + code + "\n")
        self.repl.exec(code, timeout=timeout, on_line=on_line)
        self.log.flush()
        return records, waves

    def one(self, code, kind, timeout=60.0):
        records, waves = self.call(code, timeout)
        found = [r for r in records if r["t"] == kind]
        if not found:
            raise ReplError("no {} record from: {}".format(kind, code))
        return found[0], waves

    def put(self, path, data):
        """Write ``data`` to ``path`` on the drive (record mode only)."""
        folder = path.rsplit("/", 1)[0]
        code = ["import binascii, os"]
        if folder:
            code.append("try:\n os.mkdir({!r})\nexcept OSError:\n pass".format(folder))
        code.append("f = open({!r}, 'wb')".format(path))
        self.repl.exec("\n".join(code))
        for i in range(0, len(data), 3072):
            chunk = base64.b64encode(data[i:i + 3072]).decode()
            self.repl.exec("f.write(binascii.a2b_base64({!r}))".format(chunk))
        self.repl.exec("f.close()\nprint(os.stat({!r})[6])".format(path))

    def size(self, path):
        out = self.repl.exec(
            "import os\ntry:\n print(os.stat({!r})[6])\nexcept OSError:\n print(-1)".format(path))
        return int(out.strip() or -1)


# --- Report ---


class Report:
    def __init__(self, folder):
        self.folder = folder
        self.rows = []

    def add(self, plan_id, check, result, detail=""):
        self.rows.append((plan_id, check, result, detail))
        mark = {"PASS": "ok  ", "FAIL": "FAIL", "WARN": "warn", "INFO": "info"}[result]
        print("  [{}] {} {}: {}".format(mark, plan_id, check, detail))

    def save_wav(self, name, samples):
        path = os.path.join(self.folder, "audio", name.replace(" ", "_").replace("/", "-") + ".wav")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        A.write_wav(path, samples)
        return os.path.relpath(path, self.folder)

    def write(self, title):
        counts = {k: sum(1 for r in self.rows if r[2] == k) for k in ("PASS", "FAIL", "WARN", "INFO")}
        lines = ["# " + title, "",
                 "{PASS} passed, {FAIL} failed, {WARN} to look at, {INFO} for information.".format(**counts),
                 "", "| Plan | Check | Result | Detail |", "| --- | --- | --- | --- |"]
        for plan_id, check, result, detail in self.rows:
            lines.append("| {} | {} | {} | {} |".format(plan_id, check, result, str(detail).replace("|", "/")))
        lines += ["", "Recordings are in audio/. log.txt has every line the mic printed."]
        path = os.path.join(self.folder, "report.md")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path, counts


# --- The automatic suite ---


def synthetic_presets():
    """Presets that isolate one feature each, with what to measure."""
    sine, off = A.SINE_NOTES, A.OFF_NOTES
    return [
        ("4.1", "Autotune chromatic, hard", "tune", off,
         {"list": [{"effect": "autotune", "key": "C", "scale": "chromatic", "speed": 1.0, "humanize": 0}]},
         {"key": "C", "scale": "chromatic"}),
        ("4.2", "Autotune C major", "tune", off,
         {"list": [{"effect": "autotune", "key": "C", "scale": "major", "speed": 1.0, "humanize": 0}]},
         {"key": "C", "scale": "major"}),
        ("4.4", "Harmonizer major, no autotune", "harmony", sine,
         {"list": [{"effect": "harmonizer", "chord": "major", "level": 0.8}]},
         {"spec": {"chord": "major"}}),
        ("4.5", "Harmonizer maj7 with autotune", "harmony", sine,
         {"list": [{"effect": "autotune", "speed": 1.0},
                   {"effect": "harmonizer", "chord": "maj7", "level": 0.8}]},
         {"spec": {"chord": "maj7"}}),
        ("4.6", "Harmonizer in the key of C major", "harmony", sine,
         {"list": [{"effect": "autotune", "key": "C", "scale": "major", "speed": 1.0},
                   {"effect": "harmonizer", "chord": "key_triad", "level": 0.8}]},
         {"spec": {"chord": "key_triad"}, "key": "C", "scale": "major"}),
        ("4.7", "Harmonizer major, 2nd inversion", "harmony", sine,
         {"list": [{"effect": "harmonizer", "chord": "major", "inversion": 2, "level": 0.8}]},
         {"spec": {"chord": "major", "inversion": 2}}),
        ("4.8", "Harmonizer level on the handle, released", "harmony_off", sine,
         {"list": [{"effect": "harmonizer", "chord": "major", "level": "$handle"}]},
         {"handle": 0.0}),
        ("4.9", "Granular pitch shift +7", "shift", sine,
         {"list": [{"effect": "granular_pitch_shift", "semitones": 7, "mix": 1}]},
         {"semitones": 7}),
        ("4.9", "Pitch shift +5", "shift", sine,
         {"list": [{"effect": "pitch_shift", "semitones": 5, "mix": 1}]},
         {"semitones": 5}),
        ("4.9", "Filter low pass 1 kHz", "plays", sine,
         {"list": [{"effect": "filter", "filter": {"mode": "low_pass", "frequency": 1000, "Q": 0.7071}, "mix": 1}]}, {}),
        ("4.9", "Chorus", "plays", sine,
         {"list": [{"effect": "chorus", "voices": 3, "delay_ms": 30, "mix": 0.5, "max_delay_ms": 50}]}, {}),
        ("4.9", "Echo", "plays", sine,
         {"list": [{"effect": "echo", "delay_ms": 250, "decay": 0.3, "mix": 0.3, "max_delay_ms": 500}]}, {}),
        ("4.9", "Freeverb", "plays", sine,
         {"list": [{"effect": "freeverb", "roomsize": 0.7, "damp": 0.5, "mix": 0.4}]}, {}),
        ("4.9", "Distortion", "plays", sine,
         {"list": [{"effect": "distortion", "drive": 0.5, "pre_gain": 6, "post_gain": -6, "mode": "overdrive", "mix": 1}]}, {}),
    ]


def run_on_device(dev, preset, wav_name, seconds, name, handle=1.0):
    code = ("import json, selftest\n"
            "selftest.run(json.loads({!r}), '/selftest/{}', seconds={}, handle={}, name={!r})").format(
        json.dumps(preset), wav_name, round(seconds, 2), handle, name)
    rec, waves = dev.one(code, "run", timeout=seconds + 60)
    return rec, waves.get(name)


def cpu_detail(rec):
    parts = []
    if rec.get("python_us_mean") is not None:
        parts.append("autotune Python {:.0f}% of each block (worst {:.0f}%)".format(
            100 * rec["python_us_mean"] / rec["block_us"], 100 * rec["python_us_max"] / rec["block_us"]))
    if rec.get("spare") is not None:
        parts.append("{:.0f}% CPU left for Python".format(100 * min(1.0, rec["spare"])))
    if rec.get("expected_blocks") and rec.get("python_us_mean") is not None:
        parts.append("kept up {}/{} blocks".format(rec["blocks"], rec["expected_blocks"]))
    if rec.get("overflow"):
        parts.append("{} mic overflows".format(rec["overflow"]))
    return ", ".join(parts)


def cpu_result(rec):
    if rec.get("overflow"):
        return "FAIL"
    if rec.get("expected_blocks") and rec.get("python_us_mean") is not None \
            and rec["blocks"] < 0.95 * rec["expected_blocks"]:
        return "FAIL"
    if rec.get("python_us_max") and rec["python_us_max"] > 0.9 * rec["block_us"]:
        return "WARN"
    if rec.get("spare") is not None and rec["spare"] < 0.2:
        return "WARN"
    return "PASS"


def check(args):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder = os.path.join(args.out, stamp)
    os.makedirs(folder, exist_ok=True)
    report = Report(folder)
    port = args.port or find_port()
    print("Mic on {}. Results in {}".format(port, folder))
    repl = RawRepl(port)
    with open(os.path.join(folder, "log.txt"), "w") as log:
        try:
            repl.enter()
            dev = Device(repl, log)
            suite(dev, report, args)
        except ReplError as error:
            report.add("-", "Suite stopped", "FAIL", str(error).splitlines()[-1][:300])
            log.write("ERROR " + str(error) + "\n")
        finally:
            try:
                repl.exec("import selftest\nselftest.done()", timeout=10)
            except Exception:
                pass
            repl.leave(restart=True)
    path, counts = report.write("Ting automatic checks, " + stamp)
    print("\n{PASS} passed, {FAIL} failed, {WARN} to look at. Report: ".format(**counts) + path)
    return 1 if counts["FAIL"] else 0


def suite(dev, report, args):
    # --- Files the checks need ---
    if dev.size("/selftest.py") != os.path.getsize(os.path.join(FIRMWARE, "selftest.py")) or args.push:
        writable = "True" in dev.repl.exec("import storage\nprint(not storage.getmount('/').readonly)")
        if not writable:
            raise ReplError("The drive is in edit mode, so the mic can't save test files. "
                            "Copy firmware/selftest.py to CIRCUITPY, or power on without TOP held.")
        with open(os.path.join(FIRMWARE, "selftest.py"), "rb") as f:
            dev.put("/selftest.py", f.read())
    signals = A.signals()
    for name, samples in signals.items():
        data = A.wav_bytes(samples)
        if dev.size("/selftest/" + name) != len(data):
            print("  putting {} on the mic".format(name))
            dev.put("/selftest/" + name, data)

    # --- 3.1 What's running ---
    info, _ = dev.one("import selftest\nselftest.info()", "info")
    uname = info.get("uname") or ["", "", "", ""]
    release = str(uname[2])
    report.add("3.1", "CircuitPython", "PASS" if release.startswith("10.") else "WARN",
               "{} ({})".format(release, str(uname[3])[:80]))
    report.add("3.1", "GranularPitchShift available",
               "PASS" if info["modules"].get("GranularPitchShift") else "FAIL",
               "needed for autotune and the harmonizer")
    report.add("3.1", "config.json loads", "FAIL" if info.get("config_error") else "PASS",
               info.get("config_error") or ", ".join(info.get("presets", [])))
    report.add("3.1", "Free memory", "INFO", "{} KB free, {} KB free on the drive".format(
        info["mem_free"] // 1024, info["drive_free"] // 1024))

    # --- Mic at rest ---
    rest, _ = dev.one("import selftest\nselftest.mic_level(1.0)", "mic_level")
    if rest["samples"] == 0:
        report.add("3.4", "Mic delivers audio", "FAIL", "no samples from the mic")
    else:
        floor = 20 * A.np.log10(rest["rms"] / 32768 + 1e-12)
        result = "PASS" if -85 < floor < -35 else "WARN"
        report.add("3.4", "Mic noise floor", result, "{:.0f} dBFS, peak {}, DC {:.0f}".format(
            floor, rest["peak"], rest["dc"]))

    # --- Pitch detection on the mic's CPU, no real time ---
    found, _ = dev.one("import selftest\nselftest.detect('/selftest/notes_off.wav')", "detect", timeout=120)
    hz = A.np.array(found["hz"])
    hits = 0
    for index, (midi, cents) in enumerate(A.OFF_NOTES):
        start = A.LEAD_IN + index * (A.NOTE_SECONDS + A.GAP_SECONDS)
        mid = slice(int((start + 0.2) * A.RATE / 256), int((start + A.NOTE_SECONDS - 0.1) * A.RATE / 256))
        seen = hz[mid]
        seen = seen[seen > 0]
        if len(seen) and abs(A.hz_midi(A.np.median(seen)) - (midi + cents / 100)) < 0.15:
            hits += 1
    report.add("4.1", "Pitch detector finds the test notes", "PASS" if hits == len(A.OFF_NOTES) else "FAIL",
               "{}/{} notes within 15 cents; slowest block {} ms of {} ms".format(
                   hits, len(A.OFF_NOTES), found["detect_ms_max"], found["block_ms"]))

    # --- Loopback: speaker to mic, no effects ---
    secs = {name: A.seconds_of(s) for name, s in signals.items()}
    rec, take = run_on_device(dev, {}, "notes_sine.wav", secs["notes_sine.wav"], "clean")
    if not rec["ok"]:
        report.add("3.4", "Clean loopback", "FAIL", rec.get("error"))
        return
    share, frames = A.heard_notes(take, A.SINE_NOTES) if take is not None else (0, 0)
    detail = "{:.0f}% of {} steady frames on the test notes, {}".format(
        100 * share, frames, A.describe(take) if take is not None else "no recording")
    if take is not None:
        report.save_wav("clean", take)
    loop_ok = share >= 0.5
    report.add("3.4", "Speaker to mic loopback, clean", "PASS" if loop_ok else "FAIL", detail)
    if not loop_ok:
        report.add("-", "Audio checks skipped", "WARN",
                   "the mic can't hear its speaker; unplug headphones and turn the knob up, "
                   "or listen to audio/clean.wav")
    clean_level = A.level_db(take) if take is not None else None

    # --- One feature at a time ---
    for plan_id, title, kind, notes, preset, opts in synthetic_presets():
        wav_name = "notes_off.wav" if notes is A.OFF_NOTES else "notes_sine.wav"
        rec, take = run_on_device(dev, preset, wav_name, secs[wav_name], title,
                                  handle=opts.get("handle", 1.0))
        if not rec["ok"]:
            report.add(plan_id, title, "FAIL", rec.get("error"))
            continue
        report.add(plan_id, title + ": CPU", cpu_result(rec), cpu_detail(rec))
        if take is None or not loop_ok:
            continue
        report.save_wav(title, take)
        if kind == "tune":
            t = A.tuning(take, opts["key"], opts["scale"])
            if not t["frames"]:
                report.add(plan_id, title, "FAIL", "no steady pitch heard")
                continue
            ok = t["median_cents"] <= 15 and t["out_of_scale"] <= 0.1
            report.add(plan_id, title, "PASS" if ok else "FAIL",
                       "median {:.0f} cents off (input was 20-40 off), {:.0f}% outside the scale, notes {}".format(
                           t["median_cents"], 100 * t["out_of_scale"], t["notes"]))
        elif kind == "harmony":
            want = A.expected_harmony(opts["spec"], notes, opts.get("key", "C"), opts.get("scale", "chromatic"))
            ok, detail = A.harmony(take, want)
            lows = [i for v in want.values() for i in v if i < -4]
            result = "PASS" if ok else ("WARN" if lows else "FAIL")
            short = "; ".join("{}: want {}{}{}".format(
                lead, d["want"], ", missing {}".format(d["missing"]) if d.get("missing") else "",
                "" if d.get("heard") else ", lead not heard") for lead, d in detail.items())
            if lows and not ok:
                short += " (notes below the lead may be too low for the speaker)"
            report.add(plan_id, title, result, short)
        elif kind == "harmony_off":
            want = {m: [4, 7] for m, _ in notes}
            heard, _ = A.harmony(take, want)
            report.add(plan_id, title, "FAIL" if heard else "PASS",
                       "backing voices heard with the handle released" if heard else "voice alone")
        elif kind == "shift":
            share, frames = A.shifted(take, notes, opts["semitones"])
            report.add(plan_id, title, "PASS" if share >= 0.5 else "FAIL",
                       "{:.0f}% of {} steady frames {} semitones up".format(100 * share, frames, opts["semitones"]))
        else:
            d = A.describe(take)
            ok = d["level_db"] > -60
            report.add(plan_id, title, "PASS" if ok else "FAIL",
                       "level {} dBFS{}; listen to the recording".format(
                           d["level_db"], "" if clean_level is None else
                           " ({:+.1f} dB against clean)".format(d["level_db"] - clean_level)))

    # --- The presets on the mic ---
    records, _ = dev.call("import selftest\nselftest.build_all(cycles=3)", timeout=120)
    for rec in records:
        if rec["t"] != "build":
            continue
        label = "Preset {} {}".format(rec["index"], rec["name"])
        if not rec["ok"]:
            report.add("3.8", label + " builds", "FAIL", rec["error"])
        else:
            result = "PASS" if rec["leak"] < 2048 else "WARN"
            report.add("3.8", label + " builds", result, "{} KB, {} bytes not given back".format(
                (rec["used"] or 0) // 1024, rec["leak"]))
    config = json.loads(dev.repl.exec("import json\nprint(json.dumps(json.load(open('/config.json'))))"))
    levels = []
    for index, preset in enumerate(config.get("presets", [])[:4]):
        title = "Preset {} {}".format(index + 1, preset.get("name", ""))
        rec, take = run_on_device(dev, preset, "notes_off.wav", secs["notes_off.wav"], title)
        if not rec["ok"]:
            report.add("4.13", title, "FAIL", rec.get("error"))
            continue
        report.add("4.13", title + ": CPU", cpu_result(rec), cpu_detail(rec))
        if take is not None and loop_ok:
            report.save_wav(title, take)
            levels.append((title, A.level_db(take)))
    if levels:
        spread = max(lv for _, lv in levels) - min(lv for _, lv in levels)
        report.add("4.13", "Default presets match in loudness", "PASS" if spread <= 6 else "WARN",
                   ", ".join("{} {:.1f} dB".format(t.split(" ", 2)[-1], lv) for t, lv in levels))

    # --- 5.1 Build and free every preset many times ---
    if args.stress:
        records, _ = dev.call("import selftest\nselftest.build_all(cycles={})".format(args.stress),
                              timeout=60 + args.stress * 10)
        for rec in records:
            if rec["t"] == "build":
                report.add("5.1", "Preset {} x{}".format(rec["index"], args.stress),
                           "PASS" if rec["ok"] and rec["leak"] < 4096 else "FAIL",
                           rec["error"] or "{} bytes not given back".format(rec["leak"]))


# --- Guided checks: needs hands and eyes ---


def ask(question):
    answer = input("  " + question + " [y/n] ").strip().lower()
    return answer.startswith("y")


def guided(args):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder = os.path.join(args.out, stamp + "_guided")
    os.makedirs(folder, exist_ok=True)
    report = Report(folder)
    repl = RawRepl(args.port or find_port())
    with open(os.path.join(folder, "log.txt"), "w") as log:
        try:
            repl.enter()
            dev = Device(repl, log)
            if dev.size("/selftest.py") < 0:
                raise ReplError("selftest.py isn't on the mic; run check or install first")
            print("\nWatch the mic: each LED lights in turn, red 1-4 then white 1-4.")
            input("  Press Enter to start.")
            dev.call("import selftest\nselftest.leds(0.6)", timeout=30)
            report.add("2.x", "LEDs light in order", "PASS" if ask("Did all eight light, in order?") else "FAIL")

            steps = [
                ("2.4", "Top button", "Press and release TOP.", lambda r: r["t"] == "button" and r["name"] == "top"),
                ("2.4", "Middle button", "Press and release MIDDLE.", lambda r: r["t"] == "button" and r["name"] == "middle"),
                ("2.4", "Bottom button", "Press and release BOTTOM.", lambda r: r["t"] == "button" and r["name"] == "bottom"),
                ("3.6", "Paddle click", "Squeeze the handle slowly until it clicks, then let go.",
                 lambda r: r["t"] == "paddle" and r["held"]),
                ("2.3", "Handle travel", "Squeeze the handle all the way in, firmly, then let go.",
                 lambda r: r["t"] == "handle"),
                ("2.5", "Volume knob", "Turn the knob from one end to the other.",
                 lambda r: r["t"] == "knob"),
            ]
            for plan_id, title, prompt, match in steps:
                print("\n" + prompt + " (10 s)")
                records, _ = dev.call("import selftest\nselftest.controls(10)", timeout=30)
                hits = [r for r in records if match(r)]
                detail = ""
                if title == "Handle travel":
                    raws = [r["raw"] for r in records if r["t"] == "handle"]
                    if raws:
                        # Each mic sits differently, so judge the swing, not fixed ends.
                        # An FX Mic swung 2520 counts, a Ting 2900.
                        swing = max(raws) - min(raws)
                        detail = "raw {} (in) to {} (out), a swing of {} counts".format(
                            min(raws), max(raws), swing)
                        hits = hits if swing >= 1500 else []
                if title == "Paddle click" and hits:
                    raw = hits[0]["handle"]
                    detail = "clicks at raw {}".format(raw)
                if title == "Volume knob":
                    raws = [r["raw"] for r in records if r["t"] == "knob"]
                    detail = "raw {} to {}".format(min(raws), max(raws)) if raws else ""
                    hits = hits if raws and max(raws) - min(raws) > 40000 else []
                report.add(plan_id, title, "PASS" if hits else "FAIL", detail or ("seen" if hits else "not seen"))
        except ReplError as error:
            report.add("-", "Guided checks stopped", "FAIL", str(error).splitlines()[-1][:300])
        finally:
            repl.leave(restart=True)
    path, _ = report.write("Ting guided checks, " + stamp)
    print("\nReport: " + path)


# --- Install and backup ---


def install(args):
    files = list(FIRMWARE_FILES)
    if args.config:
        files.insert(-1, "config.json")
    drive = find_drive()
    if drive and os.access(drive, os.W_OK) and not args.serial:
        for name in files:
            shutil.copy(os.path.join(FIRMWARE, name), os.path.join(drive, name))
            print("copied", name)
        print("Done. Power the mic off and on.")
        return
    repl = RawRepl(args.port or find_port())
    repl.enter()
    dev = Device(repl, open(os.devnull, "w"))
    try:
        if "True" not in repl.exec("import storage\nprint(not storage.getmount('/').readonly)"):
            raise SystemExit("The mic is in edit mode but its drive wasn't found to copy to.")
        for name in files:
            with open(os.path.join(FIRMWARE, name), "rb") as f:
                dev.put("/" + name, f.read())
            print("sent", name)
    finally:
        repl.leave(restart=True)
    print("Done. code.py restarted.")


def backup(args):
    drive = find_drive()
    if not drive:
        raise SystemExit("Couldn't find the mic's drive. Is it plugged in and switched on?")
    dest = os.path.join(args.dest, "ting-backup-" + datetime.date.today().isoformat())
    shutil.copytree(drive, dest, ignore=shutil.ignore_patterns(
        ".Trashes", ".fseventsd", ".Spotlight-V100", "System Volume Information", "._*"))
    print("Copied {} to {}:".format(drive, dest))
    for path in sorted(glob.glob(os.path.join(dest, "**"), recursive=True)):
        if os.path.isfile(path):
            print("  {} ({} bytes)".format(os.path.relpath(path, dest), os.path.getsize(path)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", help="serial port, if it isn't found by itself")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="automatic checks, no hands needed")
    p.add_argument("--out", default=os.path.join(ROOT, "test-results"))
    p.add_argument("--stress", type=int, default=0, help="build every preset this many times")
    p.add_argument("--push", action="store_true", help="send selftest.py even if it looks current")
    p = sub.add_parser("guided", help="LEDs, buttons, handle and knob, with prompts")
    p.add_argument("--out", default=os.path.join(ROOT, "test-results"))
    p = sub.add_parser("install", help="copy fx-mic-mod to the mic")
    p.add_argument("--config", action="store_true", help="also replace config.json")
    p.add_argument("--serial", action="store_true", help="send over serial even if the drive is writable")
    p = sub.add_parser("backup", help="copy the mic's drive to a folder")
    p.add_argument("dest")
    args = parser.parse_args(argv)
    return {"check": check, "guided": guided, "install": install, "backup": backup}[args.command](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
