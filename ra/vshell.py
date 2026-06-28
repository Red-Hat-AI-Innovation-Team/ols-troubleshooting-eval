"""Minimal virtual shell backed by in-memory filesystem and network."""

import re
import shlex
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class VFile:
  content: str | None = None
  is_directory: bool = False
  permissions: str = "-rw-r--r--"
  owner: str = "root"
  size_bytes: int = 0


@dataclass
class VDNSRecord:
  ip: str | None = None


@dataclass
class VHTTPEndpoint:
  status_code: int = 200
  body: str = ""
  headers: dict[str, str] = field(default_factory=dict)
  error: str | None = None


@dataclass
class VNet:
  dns: dict[str, VDNSRecord] = field(default_factory=dict)
  http: dict[str, VHTTPEndpoint] = field(default_factory=dict)


def vfile_from_dict(d: dict) -> VFile:
  return VFile(
    content=d.get("content"),
    is_directory=d.get("is_directory", False),
    permissions=d.get("permissions", "-rw-r--r--"),
    owner=d.get("owner", "root"),
    size_bytes=d.get("size_bytes", 0),
  )


def vnet_from_dict(d: dict) -> VNet:
  dns = {}
  for k, v in d.get("dns", {}).items():
    dns[k] = VDNSRecord(ip=v.get("ip"))
  http = {}
  for k, v in d.get("http", {}).items():
    http[k] = VHTTPEndpoint(
      status_code=v.get("status_code", 200),
      body=v.get("body", ""),
      headers=v.get("headers", {}),
      error=v.get("error"),
    )
  return VNet(dns=dns, http=http)


class VShell:
  """Execute commands against an in-memory virtual filesystem."""

  def __init__(
    self,
    fs: dict[str, VFile],
    hostname: str = "localhost",
    net: VNet | None = None,
  ):
    self.fs = fs
    self.hostname = hostname
    self.net = net or VNet()
    self.cwd = "/"

  # ---- public API ----

  def run(self, commands: list[str]) -> str | None:
    for cmd_str in commands:
      stages = [s.strip() for s in cmd_str.split("|")]
      output: str | None = None
      for stage in stages:
        try:
          parts = shlex.split(stage)
        except ValueError:
          parts = stage.split()
        if not parts:
          continue
        output = self._dispatch(parts, stdin=output)
        if output is None:
          return None
    return output

  # ---- dispatcher ----

  def _dispatch(self, parts: list[str], stdin: str | None = None) -> str | None:
    cmd = parts[0]
    args = parts[1:]
    table: dict[str, Any] = {
      "cat": self._cat,
      "ls": self._ls,
      "head": self._head,
      "tail": self._tail,
      "grep": self._grep,
      "egrep": self._grep,
      "wc": self._wc,
      "echo": self._echo,
      "hostname": self._hostname_cmd,
      "pwd": self._pwd,
      "env": self._env,
      "printenv": self._env,
      "find": self._find,
      "sort": self._sort,
      "uniq": self._uniq,
      "tr": self._tr,
      "cut": self._cut,
      "awk": self._awk_simple,
      "xargs": self._xargs,
      "test": self._test,
      "[": self._test,
      "curl": self._curl,
      "wget": self._wget,
      "nslookup": self._nslookup,
      "dig": self._dig,
      "true": lambda a, s: "",
      "false": lambda a, s: None,
      "id": lambda a, s: "uid=0(root) gid=0(root) groups=0(root)",
      "whoami": lambda a, s: "root",
      "date": lambda a, s: "Mon Jan  1 00:00:00 UTC 2024",
    }
    if cmd in table:
      return table[cmd](args, stdin)
    return f"bash: {cmd}: command not found"

  # ---- filesystem helpers ----

  def _resolve(self, path: str) -> str:
    if not path.startswith("/"):
      path = self.cwd.rstrip("/") + "/" + path
    parts = []
    for p in path.split("/"):
      if p == "" or p == ".":
        continue
      elif p == "..":
        if parts:
          parts.pop()
      else:
        parts.append(p)
    return "/" + "/".join(parts)

  def _read_file(self, path: str) -> str | None:
    resolved = self._resolve(path)
    entry = self.fs.get(resolved)
    if entry and not entry.is_directory:
      return entry.content
    return None

  def _is_dir(self, path: str) -> bool:
    resolved = self._resolve(path)
    entry = self.fs.get(resolved)
    if entry and entry.is_directory:
      return True
    prefix = resolved.rstrip("/") + "/"
    return any(p.startswith(prefix) for p in self.fs)

  def _list_dir(self, path: str) -> list[tuple[str, VFile]] | None:
    resolved = self._resolve(path).rstrip("/")
    if resolved == "":
      resolved = "/"
    prefix = resolved + "/" if resolved != "/" else "/"

    entries: dict[str, tuple[str, VFile]] = {}

    for p, vf in self.fs.items():
      if not p.startswith(prefix):
        continue
      rel = p[len(prefix):]
      if not rel:
        continue
      child_name = rel.split("/")[0]
      child_path = prefix + child_name

      if "/" in rel:
        if child_name not in entries:
          entries[child_name] = (child_path, VFile(is_directory=True, permissions="drwxr-xr-x"))
      else:
        entries[child_name] = (p, vf)

    if not entries:
      return None

    return list(entries.values())

  # ---- command implementations ----

  def _cat(self, args: list[str], stdin: str | None) -> str | None:
    if not args:
      return stdin
    results = []
    for a in args:
      if a.startswith("-"):
        continue
      content = self._read_file(a)
      if content is not None:
        results.append(content)
      else:
        results.append(f"cat: {a}: No such file or directory")
    return "\n".join(results) if results else None

  def _ls(self, args: list[str], stdin: str | None) -> str | None:
    long_fmt = False
    all_files = False
    paths = []
    for a in args:
      if a.startswith("-"):
        if "l" in a:
          long_fmt = True
        if "a" in a:
          all_files = True
      else:
        paths.append(a)
    if not paths:
      paths = [self.cwd]

    lines = []
    for p in paths:
      entries = self._list_dir(p)
      if entries is None:
        lines.append(f"ls: cannot access '{p}': No such file or directory")
        continue
      for fpath, vf in sorted(entries, key=lambda x: x[0]):
        name = fpath.rsplit("/", 1)[-1]
        if not all_files and name.startswith("."):
          continue
        if vf.is_directory:
          name += "/"
        if long_fmt:
          perms = vf.permissions
          owner = vf.owner
          size = vf.size_bytes
          lines.append(f"{perms} {owner} {size:>8} {name}")
        else:
          lines.append(name)
    return "\n".join(lines) if lines else ""

  def _head(self, args: list[str], stdin: str | None) -> str | None:
    n, path = 10, None
    i = 0
    while i < len(args):
      if args[i] == "-n" and i + 1 < len(args):
        n = int(args[i + 1]); i += 2
      elif args[i].startswith("-") and args[i][1:].isdigit():
        n = int(args[i][1:]); i += 1
      else:
        path = args[i]; i += 1
    text = self._read_file(path) if path else stdin
    if text is None:
      return None
    return "\n".join(text.split("\n")[:n])

  def _tail(self, args: list[str], stdin: str | None) -> str | None:
    n, path = 10, None
    i = 0
    while i < len(args):
      if args[i] == "-n" and i + 1 < len(args):
        n = int(args[i + 1]); i += 2
      elif args[i].startswith("-") and args[i][1:].isdigit():
        n = int(args[i][1:]); i += 1
      else:
        path = args[i]; i += 1
    text = self._read_file(path) if path else stdin
    if text is None:
      return None
    return "\n".join(text.split("\n")[-n:])

  def _grep(self, args: list[str], stdin: str | None) -> str | None:
    ignore_case = False
    invert = False
    count_only = False
    pattern = None
    files = []
    i = 0
    while i < len(args):
      a = args[i]
      if a.startswith("-") and not a.startswith("--"):
        if "i" in a: ignore_case = True
        if "v" in a: invert = True
        if "c" in a: count_only = True
        if "E" in a: pass
        i += 1
      elif pattern is None:
        pattern = a; i += 1
      else:
        files.append(a); i += 1

    if pattern is None:
      return None

    flags = re.IGNORECASE if ignore_case else 0
    try:
      regex = re.compile(pattern, flags)
    except re.error:
      regex = re.compile(re.escape(pattern), flags)

    text = stdin
    if files:
      parts = []
      for f in files:
        c = self._read_file(f)
        if c is not None:
          parts.append(c)
      text = "\n".join(parts) if parts else None

    if text is None:
      return None

    matches = []
    for line in text.split("\n"):
      hit = regex.search(line) is not None
      if hit != invert:
        matches.append(line)

    if count_only:
      return str(len(matches))
    return "\n".join(matches)

  def _wc(self, args: list[str], stdin: str | None) -> str | None:
    line_only = "-l" in args
    files = [a for a in args if not a.startswith("-")]
    text = stdin
    if files:
      parts = []
      for f in files:
        c = self._read_file(f)
        if c:
          parts.append(c)
      text = "\n".join(parts)
    if text is None:
      return None
    lines = text.split("\n")
    if line_only:
      return str(len(lines))
    words = len(text.split())
    chars = len(text)
    return f"{len(lines)} {words} {chars}"

  def _echo(self, args: list[str], stdin: str | None) -> str | None:
    return " ".join(args)

  def _hostname_cmd(self, args: list[str], stdin: str | None) -> str | None:
    return self.hostname

  def _pwd(self, args: list[str], stdin: str | None) -> str | None:
    return self.cwd

  def _env(self, args: list[str], stdin: str | None) -> str | None:
    content = self._read_file("/.env")
    return content if content else ""

  def _find(self, args: list[str], stdin: str | None) -> str | None:
    path = "/"
    name_pattern = None
    type_filter = None
    i = 0
    while i < len(args):
      a = args[i]
      if a == "-name" and i + 1 < len(args):
        name_pattern = args[i + 1]; i += 2
      elif a == "-type" and i + 1 < len(args):
        type_filter = args[i + 1]; i += 2
      elif not a.startswith("-"):
        path = a; i += 1
      else:
        i += 1

    prefix = self._resolve(path).rstrip("/") + "/"
    target = self._resolve(path)

    all_paths: dict[str, VFile] = {}
    for p, vf in self.fs.items():
      all_paths[p] = vf
      parts = p.strip("/").split("/")
      for j in range(1, len(parts)):
        dir_path = "/" + "/".join(parts[:j])
        if dir_path not in all_paths:
          all_paths[dir_path] = VFile(is_directory=True, permissions="drwxr-xr-x")

    results = []
    for p, vf in all_paths.items():
      if p != target and not p.startswith(prefix):
        continue
      if type_filter == "f" and vf.is_directory:
        continue
      if type_filter == "d" and not vf.is_directory:
        continue
      if name_pattern:
        fname = p.rsplit("/", 1)[-1]
        pat = name_pattern.replace("*", ".*").replace("?", ".")
        if not re.match(pat, fname):
          continue
      results.append(p)
    return "\n".join(sorted(results))

  def _sort(self, args: list[str], stdin: str | None) -> str | None:
    reverse = "-r" in args
    files = [a for a in args if not a.startswith("-")]
    text = stdin
    if files:
      text = self._read_file(files[0])
    if text is None:
      return None
    lines = text.split("\n")
    return "\n".join(sorted(lines, reverse=reverse))

  def _uniq(self, args: list[str], stdin: str | None) -> str | None:
    count = "-c" in args
    text = stdin
    if text is None:
      return None
    lines = text.split("\n")
    result = []
    prev = None
    cnt = 0
    for line in lines:
      if line == prev:
        cnt += 1
      else:
        if prev is not None:
          result.append(f"{cnt:>4} {prev}" if count else prev)
        prev = line
        cnt = 1
    if prev is not None:
      result.append(f"{cnt:>4} {prev}" if count else prev)
    return "\n".join(result)

  def _tr(self, args: list[str], stdin: str | None) -> str | None:
    if stdin is None or len(args) < 2:
      return stdin
    delete = "-d" in args
    non_flag = [a for a in args if not a.startswith("-")]
    if delete and non_flag:
      chars = non_flag[0]
      return stdin.translate(str.maketrans("", "", chars))
    if len(non_flag) >= 2:
      return stdin.translate(str.maketrans(non_flag[0], non_flag[1]))
    return stdin

  def _cut(self, args: list[str], stdin: str | None) -> str | None:
    if stdin is None:
      return None
    delim = "\t"
    fields = []
    i = 0
    while i < len(args):
      if args[i] == "-d" and i + 1 < len(args):
        delim = args[i + 1]; i += 2
      elif args[i] == "-f" and i + 1 < len(args):
        for p in args[i + 1].split(","):
          if "-" in p:
            s, _, e = p.partition("-")
            s = int(s) if s else 1
            e = int(e) if e else 999
            fields.extend(range(s, e + 1))
          else:
            fields.append(int(p))
        i += 2
      else:
        i += 1
    result = []
    for line in stdin.split("\n"):
      parts = line.split(delim)
      selected = [parts[f - 1] if f - 1 < len(parts) else "" for f in fields]
      result.append(delim.join(selected))
    return "\n".join(result)

  def _awk_simple(self, args: list[str], stdin: str | None) -> str | None:
    if stdin is None or not args:
      return stdin
    prog = args[0]
    m = re.match(r"\{print\s+(\$\d+(?:\s*,\s*\$\d+)*)\}", prog.strip())
    if not m:
      return stdin
    field_refs = re.findall(r"\$(\d+)", m.group(1))
    indices = [int(f) for f in field_refs]
    result = []
    for line in stdin.split("\n"):
      parts = line.split()
      selected = []
      for idx in indices:
        if idx == 0:
          selected.append(line)
        elif idx - 1 < len(parts):
          selected.append(parts[idx - 1])
      result.append(" ".join(selected))
    return "\n".join(result)

  def _xargs(self, args: list[str], stdin: str | None) -> str | None:
    if stdin is None or not args:
      return stdin
    items = stdin.split()
    return self._dispatch(args + items, stdin=None)

  def _test(self, args: list[str], stdin: str | None) -> str | None:
    clean = [a for a in args if a != "]"]
    if len(clean) >= 2 and clean[0] == "-f":
      content = self._read_file(clean[1])
      return "" if content is not None else None
    if len(clean) >= 2 and clean[0] == "-d":
      return "" if self._is_dir(clean[1]) else None
    return ""

  # ---- network commands ----

  def _parse_url(self, url: str) -> tuple[str, int, str]:
    if "://" not in url:
      url = "http://" + url
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    return host, port, path

  def _net_resolve(self, host: str) -> str | None:
    rec = self.net.dns.get(host)
    if rec is None:
      return None
    return rec.ip

  def _net_http(self, host: str, port: int, path: str) -> VHTTPEndpoint | None:
    keys = [
      f"{host}:{port}{path}",
      f"{host}:{port}",
      host,
    ]
    for k in keys:
      if k in self.net.http:
        return self.net.http[k]
    return None

  def _curl(self, args: list[str], stdin: str | None) -> str | None:
    include_headers = False
    url = None
    i = 0
    while i < len(args):
      a = args[i]
      if a in ("-i", "-I", "--include"):
        include_headers = True; i += 1
      elif a in ("-s", "--silent"):
        i += 1
      elif a in ("-o", "-X", "-H", "--header", "-d", "--data",
                  "--connect-timeout", "--max-time", "-m", "-u", "--user"):
        i += 2
      elif a.startswith("-"):
        i += 1
      else:
        url = a; i += 1

    if not url:
      return "curl: no URL specified"

    host, port, path = self._parse_url(url)

    if not host:
      return "curl: (3) URL rejected: No host part in the URL"

    ip = self._net_resolve(host)
    if ip is None:
      return f"curl: (6) Could not resolve host: {host}"

    endpoint = self._net_http(host, port, path)
    if endpoint is None:
      return f"curl: (7) Failed to connect to {host} port {port}: Connection refused"

    if endpoint.error:
      return f"curl: (7) Failed to connect to {host} port {port}: {endpoint.error}"

    lines = []
    if include_headers:
      lines.append(f"HTTP/1.1 {endpoint.status_code}")
      for k, v in endpoint.headers.items():
        lines.append(f"{k}: {v}")
      lines.append("")
    lines.append(endpoint.body)
    return "\n".join(lines)

  def _wget(self, args: list[str], stdin: str | None) -> str | None:
    url = None
    i = 0
    while i < len(args):
      a = args[i]
      if a in ("-q", "--quiet"):
        i += 1
      elif a in ("-O", "--output-document"):
        i += 2
      elif a.startswith("-"):
        i += 1
      else:
        url = a; i += 1

    if not url:
      return "wget: missing URL"

    host, port, path = self._parse_url(url)
    if not host:
      return f"wget: unable to resolve host address '{url}'"

    ip = self._net_resolve(host)
    if ip is None:
      return f"wget: unable to resolve host address '{host}'"

    endpoint = self._net_http(host, port, path)
    if endpoint is None:
      return "wget: failed: Connection refused."

    if endpoint.error:
      return f"wget: failed: {endpoint.error}"

    return endpoint.body

  def _nslookup(self, args: list[str], stdin: str | None) -> str | None:
    host = None
    for a in args:
      if not a.startswith("-"):
        host = a
        break
    if not host:
      return "nslookup: no host specified"

    ip = self._net_resolve(host)
    if ip is None:
      return f"** server can't find {host}: NXDOMAIN"

    return (
      f"Server:\t\t172.30.0.10\n"
      f"Address:\t172.30.0.10#53\n"
      f"\n"
      f"Name:\t{host}\n"
      f"Address: {ip}"
    )

  def _dig(self, args: list[str], stdin: str | None) -> str | None:
    host = None
    for a in args:
      if not a.startswith("-") and not a.startswith("@"):
        host = a
        break
    if not host:
      return "dig: no host specified"

    ip = self._net_resolve(host)
    if ip is None:
      return ";; connection timed out; no servers could be reached"

    return (
      f"; <<>> DiG <<>> {host}\n"
      f";; ANSWER SECTION:\n"
      f"{host}.\t\t30\tIN\tA\t{ip}\n"
      f"\n"
      f";; Query time: 1 msec\n"
      f";; SERVER: 172.30.0.10#53(172.30.0.10)"
    )
