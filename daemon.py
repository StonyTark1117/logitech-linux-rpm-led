"""Headless daemon mode for the Logitech RPM LED indicator.

Skips the GTK UI entirely. Reads the same settings.ini the GUI writes,
finds the wheel, and either runs telemetry for a fixed game choice or
polls for running games and switches automatically. Intended to be
launched by systemd as a user service.
"""
from __future__ import annotations

import configparser
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from games.assetto_corsa import AssettoCorsa
from games.automobilista_2 import Automobilista2
from games.autodetect import detect_running_game
from games.dirt_rally_2_0 import DirtRally2
from games.f12019 import F12019
from games.f12020 import F12020
from games.f12022 import F12022
from games.f12023 import F12023
from games.forza_horizon import ForzaHorizon5
from wheels.base import BaseWheel
from wheels.detect import find_wheel

FORZA_HORIZON_5 = 0
F1_2019 = 1
F1_2020 = 2
F1_2022 = 3
F1_2023 = 4
DIRT_RALLY_2_0 = 5
AMS_2 = 6
ASSETTO_CORSA = 7

GAME_KEY_TO_CHOICE = {
    "forza_horizon_5": FORZA_HORIZON_5,
    "f1_2019": F1_2019,
    "f1_2020": F1_2020,
    "f1_2022": F1_2022,
    "f1_2023": F1_2023,
    "dirt_rally_2_0": DIRT_RALLY_2_0,
    "ams_2": AMS_2,
    "assetto_corsa": ASSETTO_CORSA,
}

DEFAULT_ASSETTO_MAX_RPM = 9000
RECONNECT_DELAY_SECONDS = 1.0
RECONNECT_INACTIVITY_SECONDS = 3.0
AUTO_DETECT_INTERVAL_SECONDS = 1.0
LED_SEND_INTERVAL_SECONDS = 0.05


def _settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "logitech-rpm-indicator" / "settings.ini"


def _load_settings() -> dict:
    config = {
        "auto_detect": True,
        "last_game": FORZA_HORIZON_5,
        "assetto_max_rpm": DEFAULT_ASSETTO_MAX_RPM,
        "thresholds": tuple(BaseWheel.DEFAULT_SHIFT_LIGHT_THRESHOLDS),
    }
    path = _settings_path()
    if not path.exists():
        print(f"[daemon] no settings file at {path}, using defaults", file=sys.stderr)
        return config
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        config["auto_detect"] = parser.getboolean("general", "auto_detect", fallback=True)
        config["last_game"] = parser.getint("general", "last_selected_game", fallback=FORZA_HORIZON_5)
        config["assetto_max_rpm"] = parser.getint("assetto_corsa", "max_rpm", fallback=DEFAULT_ASSETTO_MAX_RPM)
        raw_thresholds = parser.get("shift_lights", "thresholds", fallback="")
        if raw_thresholds:
            parts = [int(p.strip()) for p in raw_thresholds.split(",") if p.strip()]
            if len(parts) == 5:
                config["thresholds"] = tuple(parts)
    except Exception as exc:
        print(f"[daemon] settings read error: {exc}", file=sys.stderr)
    return config


def _create_game(choice: int, config: dict):
    if choice == FORZA_HORIZON_5:
        return ForzaHorizon5()
    if choice == F1_2019:
        return F12019()
    if choice == F1_2020:
        return F12020()
    if choice == F1_2022:
        return F12022()
    if choice == F1_2023:
        return F12023()
    if choice == DIRT_RALLY_2_0:
        return DirtRally2()
    if choice == AMS_2:
        return Automobilista2()
    if choice == ASSETTO_CORSA:
        return AssettoCorsa(max_rpm=config["assetto_max_rpm"])
    return None


def _close_socket(udp_socket, game=None):
    if not udp_socket:
        return None
    try:
        disconnect = getattr(game, "disconnect", None) if game else None
        if callable(disconnect):
            disconnect(udp_socket)
    except Exception:
        pass
    try:
        udp_socket.close()
    except Exception:
        pass
    return None


def _telemetry_loop(game, wheel, choice, stop_event):
    udp_socket = None
    percent = 0
    last_send = 0.0
    last_packet_time = 0.0
    next_reconnect_time = 0.0
    while not stop_event.is_set():
        now = time.monotonic()
        if udp_socket is None:
            if now < next_reconnect_time:
                time.sleep(0.05)
                continue
            try:
                udp_socket = game.connect()
                udp_socket.settimeout(0.2)
                last_packet_time = time.monotonic()
                percent = 0
                print(f"[daemon] telemetry connected: {type(game).__name__}", flush=True)
            except Exception as exc:
                print(f"[daemon] connect failed: {exc}", file=sys.stderr, flush=True)
                next_reconnect_time = now + RECONNECT_DELAY_SECONDS
                time.sleep(0.05)
                continue

        try:
            data = game.read_data(udp_socket=udp_socket)
        except socket.timeout:
            if time.monotonic() - last_packet_time >= RECONNECT_INACTIVITY_SECONDS:
                udp_socket = _close_socket(udp_socket, game)
                next_reconnect_time = time.monotonic() + RECONNECT_DELAY_SECONDS
            continue
        except Exception as exc:
            print(f"[daemon] read failed, reconnecting: {exc}", file=sys.stderr, flush=True)
            udp_socket = _close_socket(udp_socket, game)
            next_reconnect_time = time.monotonic() + RECONNECT_DELAY_SECONDS
            continue

        last_packet_time = time.monotonic()
        if choice == FORZA_HORIZON_5:
            max_rpm, current_rpm = game.parse_rpm(data=data)
            percent = game.get_rpm_percent(max_rpm=max_rpm, current_rpm=current_rpm)
        else:
            percent = game.get_rpm_percent(data, percent)
        clamped = max(0, min(int(percent), 100))
        now = time.perf_counter()
        if now - last_send >= LED_SEND_INTERVAL_SECONDS:
            try:
                wheel.leds_rpm(clamped)
            except Exception as exc:
                print(f"[daemon] wheel write failed: {exc}", file=sys.stderr, flush=True)
            last_send = now

    try:
        wheel.leds_rpm(0)
    except Exception:
        pass
    _close_socket(udp_socket, game)


def main() -> int:
    config = _load_settings()
    BaseWheel.set_shift_light_thresholds(config["thresholds"])

    wheel = find_wheel()
    if not wheel:
        print("[daemon] no supported Logitech wheel found, exiting", file=sys.stderr, flush=True)
        return 1

    shutdown = threading.Event()
    telemetry_stop = threading.Event()
    state = {"thread": None, "choice": None}

    def stop_telemetry():
        telemetry_stop.set()
        thread = state["thread"]
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        state["thread"] = None
        state["choice"] = None
        try:
            wheel.leds_rpm(0)
        except Exception:
            pass

    def start_telemetry(choice):
        game = _create_game(choice, config)
        if not game:
            return
        telemetry_stop.clear()
        state["choice"] = choice
        thread = threading.Thread(
            target=_telemetry_loop,
            args=(game, wheel, choice, telemetry_stop),
            daemon=True,
        )
        thread.start()
        state["thread"] = thread

    def handle_signal(*_):
        shutdown.set()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if config["auto_detect"]:
        print("[daemon] auto-detect ON; polling for running games every 1s", flush=True)
    else:
        print(f"[daemon] auto-detect OFF; starting telemetry for choice={config['last_game']}", flush=True)
        start_telemetry(config["last_game"])

    last_check = 0.0
    while not shutdown.is_set():
        if config["auto_detect"]:
            now = time.monotonic()
            if now - last_check >= AUTO_DETECT_INTERVAL_SECONDS:
                last_check = now
                detected_key = detect_running_game()
                detected_choice = GAME_KEY_TO_CHOICE.get(detected_key)
                if detected_choice is None and state["choice"] is not None:
                    print("[daemon] game gone; stopping telemetry", flush=True)
                    stop_telemetry()
                elif detected_choice is not None and detected_choice != state["choice"]:
                    if state["choice"] is not None:
                        stop_telemetry()
                    print(f"[daemon] detected {detected_key}; starting telemetry", flush=True)
                    start_telemetry(detected_choice)
        else:
            thread = state["thread"]
            if thread is None or not thread.is_alive():
                start_telemetry(config["last_game"])
        time.sleep(0.2)

    print("[daemon] shutting down", flush=True)
    stop_telemetry()
    return 0


if __name__ == "__main__":
    sys.exit(main())
