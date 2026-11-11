#!/usr/bin/env python3
"""Настройки Docker и порты для файрвола: части server-base.sh, которые на bash вышли бы хрупкими.

  serverbase.py daemon-json ФАЙЛ [--dockerd-flags ФЛАГИ] [--swarm] [--no-new-privileges]
      дописать в настройки Docker ротацию логов и live-restore, не трогая заданное человеком
  serverbase.py firewall-ports [--listening]
      порты SSH, 80 и 443, а с --listening ещё все, что слушают не только на localhost

daemon-json печатает, что дописал, и выходит с кодом 0, если файл изменился, 10 — если менять
нечего, 11 — если файл не JSON-объект: такой файл не трогаем.
"""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile

EXIT_UNCHANGED = 10
EXIT_NOT_JSON = 11
LOG_OPTS = {"max-size": "10m", "max-file": "3"}


def merge_daemon(conf, flags="", swarm=False, no_new_privileges=False):
    """Новый словарь настроек: наши ключи добавляются, только если их нет ни в файле, ни во флагах
    dockerd — ключ, заданный и там и там, не даст Docker запуститься."""
    out = dict(conf)

    def free(key, flag):
        return key not in out and flag not in flags

    opts = out.get("log-opts", {})
    if free("log-driver", "--log-driver") and "--log-opt" not in flags and isinstance(opts, dict):
        out["log-driver"] = "json-file"
        out["log-opts"] = {**LOG_OPTS, **opts}
    # В режиме swarm Docker с live-restore не запускается.
    if not swarm and free("live-restore", "--live-restore"):
        out["live-restore"] = True
    if no_new_privileges and free("no-new-privileges", "--no-new-privileges"):
        out["no-new-privileges"] = True
    return out


def added_keys(before, after):
    return [k for k in after if before.get(k) != after[k]]


def write_atomic(path, text):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".daemon.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def daemon_json(path, flags, swarm, no_new_privileges):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    conf = {}
    if text.strip():
        try:
            conf = json.loads(text)
        except ValueError as e:
            print(f"{path} не разобрать как JSON: {e}", file=sys.stderr)
            return EXIT_NOT_JSON
        if not isinstance(conf, dict):
            print(f"{path} — не объект JSON", file=sys.stderr)
            return EXIT_NOT_JSON
    merged = merge_daemon(conf, flags, swarm, no_new_privileges)
    if merged == conf:
        return EXIT_UNCHANGED
    write_atomic(path, json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    print(", ".join(added_keys(conf, merged)))
    return 0


def ssh_ports(sshd_config, socket_listen):
    """Порты из sshd -T и из Listen у ssh.socket: с ним (Ubuntu 24.04+) порт слушает systemd."""
    ports = set()
    for line in sshd_config.splitlines():
        key, _, value = line.strip().partition(" ")
        if key == "port" and value.isdigit():
            ports.add(int(value))
        elif key == "listenaddress":
            m = re.search(r":(\d+)$", value)
            if m:
                ports.add(int(m.group(1)))
    for m in re.finditer(r"(?:^|[\s:])(\d+) \(Stream\)", socket_listen):
        ports.add(int(m.group(1)))
    return sorted(ports) or [22]


def is_local(host):
    host = host.strip("[]")
    if host.endswith("%lo"):
        return True
    host = host.split("%")[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local


def listening_ports(tcp, udp):
    """Порты из ss -H -tln и ss -H -uln, кроме тех, что слушают только на localhost."""
    ports = set()
    for proto, text in (("tcp", tcp), ("udp", udp)):
        for line in text.splitlines():
            cols = line.split()
            if len(cols) < 4:
                continue
            host, _, port = cols[3].rpartition(":")
            if port.isdigit() and not is_local(host):
                ports.add((int(port), proto))
    return [f"{port}/{proto}" for port, proto in sorted(ports)]


def run(*cmd):
    """Вывод команды; пустой, если она не запустилась или упала."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def firewall_ports(listening):
    socket = ""
    if run("systemctl", "is-active", "ssh.socket").strip() == "active":
        socket = run("systemctl", "show", "ssh.socket", "-p", "Listen", "--value")
    ports = [f"{p}/tcp" for p in ssh_ports(run("sshd", "-T"), socket)] + ["80/tcp", "443/tcp"]
    if listening:
        ports += listening_ports(run("ss", "-H", "-tln"), run("ss", "-H", "-uln"))
    return list(dict.fromkeys(ports))


def main(argv):
    parser = argparse.ArgumentParser(prog="serverbase.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    daemon = sub.add_parser("daemon-json")
    daemon.add_argument("path")
    daemon.add_argument("--dockerd-flags", default="")
    daemon.add_argument("--swarm", action="store_true")
    daemon.add_argument("--no-new-privileges", action="store_true")
    fw = sub.add_parser("firewall-ports")
    fw.add_argument("--listening", action="store_true")
    args = parser.parse_args(argv[1:])
    if args.cmd == "daemon-json":
        return daemon_json(args.path, args.dockerd_flags, args.swarm, args.no_new_privileges)
    print("\n".join(firewall_ports(args.listening)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
