"""Action audit parsing and VMware command boundary tests."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from memhall.adapters import remote
from memhall.adapters.base import AgentUnavailable
from memhall.adapters.audit import AuditdCollector, parse_audit_events
from memhall.adapters.kylinbot import KylinBotAdapter
from memhall.adapters.remote import (
    remote_environment,
    restore_remote_clock,
    shift_remote_clock,
)
from memhall.vm import VmwareManager


def test_parse_audit_events_maps_create_and_delete():
    raw = """+type=SYSCALL msg=audit(1791072000.125:41): syscall=mkdir success=yes exe="/usr/bin/mkdir"
type=PATH msg=audit(1791072000.125:41): name="/home/okim/work/demo" nametype=CREATE
----
type=SYSCALL msg=audit(1791072001.500:42): syscall=unlinkat success=yes exe="/usr/bin/rm"
type=PATH msg=audit(1791072001.500:42): name="/home/okim/work/old" nametype=DELETE
----
type=SYSCALL msg=audit(1791072002.750:43): syscall=renameat2 success=yes exe="/usr/bin/mv"
type=PATH msg=audit(1791072002.750:43): name="/home/okim/work/before" nametype=DELETE
type=PATH msg=audit(1791072002.750:43): name="/home/okim/work/after" nametype=CREATE
"""
    actions = parse_audit_events(raw)
    assert [item.tool for item in actions] == [
        "fs.create", "fs.delete", "fs.rename"]
    assert actions[0].args["paths"][0]["path"] == "/home/okim/work/demo"
    assert all(item.source.value == "auditd" for item in actions)


def test_audit_collector_reads_explicit_openkylin_log(monkeypatch):
    class Channel:
        user = "kylin"

        def __init__(self):
            self.commands = []

        def run_sudo(self, command, timeout=300):
            self.commands.append(command)
            return 0, "", ""

    monkeypatch.setenv("MEMHALL_AUDITD", "1")
    channel = Channel()
    collector = AuditdCollector(channel)
    collector.active = True
    collector.key = "mh_test"
    collector.dump()
    assert channel.commands == [
        "ausearch -if /var/log/audit/audit.log -k mh_test --raw"]


def test_remote_clock_disables_ntp_and_restores_elapsed_time(monkeypatch):
    class Channel:
        def __init__(self):
            self.sudo_calls = []

        def run(self, command, timeout=300):
            if command == "date +%s":
                return 0, "1000\n", ""
            return 0, "yes\n", ""

        def run_sudo(self, command, timeout=300):
            self.sudo_calls.append(command)
            return 0, "", ""

    ticks = iter([50.0, 57.9])
    monkeypatch.setattr(remote.time, "monotonic", lambda: next(ticks))
    channel = Channel()
    state = shift_remote_clock(channel, 3)
    restore_remote_clock(channel, state)
    assert channel.sudo_calls == [
        "timedatectl set-ntp false",
        "date -s '+3 days' >/dev/null",
        "date -s @1007 >/dev/null",
        "timedatectl set-ntp true",
    ]


def test_reboot_waits_for_boot_id_change(monkeypatch):
    class Channel:
        def __init__(self):
            self.boot_ids = iter(["boot-old\n", "boot-old\n", "boot-new\n"])
            self.closed = 0

        def run(self, command, timeout=300):
            assert command == "cat /proc/sys/kernel/random/boot_id"
            return 0, next(self.boot_ids), ""

        def run_sudo(self, command, timeout=300):
            assert command == "shutdown -r now"
            return 0, "", ""

        def close(self):
            self.closed += 1

    monkeypatch.setattr(remote.time, "sleep", lambda _: None)
    channel = Channel()
    remote.SshChannel.reboot_and_wait(channel, timeout_s=1)
    assert channel.closed >= 2


def test_remote_environment_records_measured_agent_version(monkeypatch):
    class Channel:
        def run_json(self, command, timeout=300):
            assert command.endswith("| base64 -d | python3")
            assert timeout == 30
            source = base64.b64decode(command.split()[1]).decode("utf-8")
            compile(source, "<remote-environment>", "exec")
            return {
                "kind": "remote",
                "os": "openKylin 3.0",
                "os_id": "openkylin",
            }

        def run(self, command, timeout=300):
            assert command == "kylin-bot --version 2>/dev/null"
            return 0, "kylin-bot 0.7.5\n", ""

        def host_key_sha256(self):
            return "host-key"

    monkeypatch.setenv("VM_SNAPSHOT", "agents-warm")
    info = remote_environment(
        Channel(), "kylinbot", "kylin-bot --version 2>/dev/null")
    assert info["agent_version"] == "kylin-bot 0.7.5"
    assert info["adapter"] == "kylinbot"
    assert info["vm_snapshot"] == "agents-warm"
    assert info["ssh_host_key_sha256"] == "host-key"


def test_remote_filesystem_probe_is_valid_python():
    compile(remote._FS_SNAPSHOT_SOURCE, "<remote-fs-snapshot>", "exec")


def test_kylinbot_send_surfaces_cli_stderr(monkeypatch):
    class Channel:
        user = "kylin"

        def run(self, command, timeout=300):
            assert "2>/dev/null" not in command
            return 1, "", "Error: OpenRouter API key not set"

    monkeypatch.setattr("memhall.adapters.kylinbot._send_throttle", lambda: None)
    adapter = KylinBotAdapter(Channel())
    with pytest.raises(AgentUnavailable, match="OpenRouter API key not set"):
        adapter.send("s-01", "hello")


def test_vm_manager_uses_argument_list_and_revert_sequence(
        tmp_path: Path, monkeypatch):
    vmx = tmp_path / "open kylin.vmx"
    vmrun = tmp_path / "vmrun.exe"
    vmx.write_text("config", encoding="utf-8")
    vmrun.write_text("binary", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        operation = command[3]
        if operation == "listSnapshots":
            output = "Total snapshots: 1\nagents-warm\n"
        elif operation == "list":
            output = "Total running VMs: 0\n"
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = VmwareManager(vmx, "agents-warm", vmrun)
    monkeypatch.setattr(manager, "start", lambda **kwargs: calls.append(["start-wait"]))
    manager.revert()
    revert = next(call for call in calls if "revertToSnapshot" in call)
    assert revert[-2:] == [str(vmx.resolve()), "agents-warm"]
    assert calls[-1] == ["start-wait"]


def test_deb_builder_is_frozen_and_has_acceptance_script():
    root = Path(__file__).parent.parent
    builder = (root / "scripts" / "build_deb_vm.sh").read_text(encoding="utf-8")
    acceptance = (root / "scripts" / "test_deb_vm.sh").read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in builder
    assert "uv.lock" in builder and "export" in builder and "--frozen" in builder
    assert "SOURCE_DATE_EPOCH" in builder
    assert "--no-index" in builder
    assert 'chmod -R a+rX "$target"' in builder
    assert "dpkg --print-architecture" in builder
    assert "Architecture: $ARCHITECTURE" in builder
    assert "_all" not in builder
    assert "memhall run" in acceptance and "--repeat 2" in acceptance
    assert "memhall verify" in acceptance
    assert "-name manifest.json" in acceptance
    assert "build.json" in acceptance and "deb_sha256" in acceptance
