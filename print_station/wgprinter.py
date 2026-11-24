"""Talk to the Flashforge printer on this house network.

The Adventurer 5M speaks two protocols. The JSON HTTP API on port 8898
needs the printer's serial number and its check code. The plain TCP
channel on port 8899 needs no code at all - it can report the state,
select a file, start it, pause it and stop it - but on this firmware it
cannot carry the file itself.

So the file goes up over HTTP with the check code the customer typed
in once, and the job control goes over TCP with nothing to keep. Both
    stay on the local network: no Flashforge account, no cloud, no internet upload
of the model anywhere.
"""

import json
import re
import socket
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wglocations import data_dir

TCP_PORT = 8899
HTTP_PORT = 8898
DISCOVERY_PORTS = (19000, 48899)
SERIAL_OFFSET = 0x92

APP = data_dir()
WORLDGEN = APP.parent
# The print station keeps its own settings inside its own folder.
SETTINGS_FILE = APP / "library" / "printer.json"


def load_settings() -> dict:
    """The printer details this machine keeps. Never leaves the machine."""
    if not SETTINGS_FILE.is_file():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_settings(values: dict) -> dict:
    """Write the printer details down. The check code is the only secret."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = load_settings()
    current.update(values)
    SETTINGS_FILE.write_text(json.dumps(current, indent=1), encoding="utf-8")
    return current


def discover(timeout: float = 3.0) -> list:
    """Find the printers on this network, without asking them anything."""
    found = []
    seen = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    except OSError:
        pass
    try:
        sock.bind(('', 0))
    except OSError:
        pass
    sock.settimeout(timeout)
    for port in DISCOVERY_PORTS:
        for host in ('255.255.255.255', '225.0.0.9'):
            try:
                sock.sendto(b'\x00' * 16, (host, port))
            except OSError:
                continue
    deadline = timeout
    while deadline > 0:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            break
        except OSError:
            break
        deadline -= 0.25
        if len(data) < SERIAL_OFFSET + 4:
            continue
        serial = data[SERIAL_OFFSET:SERIAL_OFFSET + 128].split(b'\x00')[0]
        serial = serial.decode('utf-8', 'replace').strip()
        if not serial or serial in seen:
            continue
        seen.add(serial)
        name = data[0:0x80].split(b'\x00')[0].decode('utf-8', 'replace').strip()
        status = struct.unpack('>H', data[0x8A:0x8C])[0]
        http_port = struct.unpack('>H', data[0x8E:0x90])[0] or HTTP_PORT
        found.append({
            "name": name or serial,
            "serial": serial,
            "address": addr[0],
            "http_port": http_port,
            "state": {0: "ready", 1: "busy", 2: "error"}.get(status, "unknown"),
        })
    sock.close()
    return found


def _local_ipv4() -> str:
    """This PC's own address on the network the printer is on."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        address = sock.getsockname()[0]
        sock.close()
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    try:
        address = socket.gethostbyname(socket.gethostname())
    except OSError:
        return ""
    return "" if address.startswith("127.") else address


# The last scan is kept for a minute, so a page that asks twice does not
# walk the network twice.
_SCAN_CACHE = {"at": 0.0, "found": []}


def scan_lan(timeout: float = 0.35, workers: int = 96,
             ttl: float = 60.0) -> list:
    """Find printers on this subnet by knocking on their own port.

    The multicast probe is the polite way to find a printer and most
    networks pass it. Some routers, some VPNs and some managed networks
    do not, and then the printer is sitting there, switched on, answering
    on its own port - and the app says no printer answered. So when the
    polite probe finds nothing, this walks the local subnet and asks each
    address directly. Nothing is sent to the printer except the question
    "who are you", and no name or code is needed to answer it.
    """
    now = time.time()
    if _SCAN_CACHE["found"] and now - _SCAN_CACHE["at"] < ttl:
        return list(_SCAN_CACHE["found"])

    local = _local_ipv4()
    if not local:
        return []
    prefix = local.rsplit(".", 1)[0]
    candidates = ["%s.%d" % (prefix, last) for last in range(1, 255)
                  if "%s.%d" % (prefix, last) != local]

    def reachable(address: str) -> bool:
        try:
            with socket.create_connection((address, TCP_PORT), timeout=timeout):
                return True
        except OSError:
            return False

    found = []
    seen = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = list(pool.map(reachable, candidates))
    for address, answered in zip(candidates, answers):
        if not answered:
            continue
        try:
            with Printer(address) as link:
                info = link.info()
        except Exception:
            continue
        serial = str(info.get("sn") or info.get("machine name") or "").strip()
        if not serial or serial in seen:
            continue
        seen.add(serial)
        machine = str(info.get("machine type") or "").strip()
        found.append({
            "name": machine or serial,
            "serial": serial,
            "address": address,
            "http_port": HTTP_PORT,
            "state": "unknown",
        })
    _SCAN_CACHE["at"] = now
    _SCAN_CACHE["found"] = found
    return list(found)


def find_printers(timeout: float = 3.0) -> list:
    """Every printer on this network, found the polite way first.

    A printer that is switched on and connected is never reported as
    missing, whether or not this network passes the multicast probe.
    """
    # The polite probe gets a short window: a printer that can answer it
    # answers in milliseconds. The subnet walk is the slower fallback, so
    # the app does not sit and wait for it.
    found = discover(min(timeout, 0.8))
    if found:
        return found
    return scan_lan()


class Printer:
    """One printer, on the plain TCP channel. No credentials needed."""

    def __init__(self, address: str, port: int = TCP_PORT, timeout: float = 6.0):
        self.address = address
        self.port = port
        self.timeout = timeout
        self._sock = None

    # -- plumbing ----------------------------------------------------------
    def open(self):
        if self._sock is None:
            self._sock = socket.create_connection((self.address, self.port),
                                                 timeout=self.timeout)
            self._sock.settimeout(self.timeout)
            self.command('M601 S1', wait=0.3)
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.sendall(b'~M602\r\n')
                time.sleep(0.2)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False

    def command(self, code: str, wait: float = 0.35) -> str:
        """Send one command and read what the printer says back.

        The printer answers with its own lines and then "ok". Waiting for
        the socket to go quiet instead of waiting for the end of the answer
        cost several seconds on every question, so the read stops the
        moment the printer says ok, or after it has gone quiet.
        """
        if self._sock is None:
            self.open()
        self._sock.sendall(('~' + code + '\r\n').encode('utf-8'))
        time.sleep(wait)
        self._sock.settimeout(0.15)
        buf = b''
        idle = 0
        deadline = time.time() + 2.5
        try:
            while time.time() < deadline:
                chunk = self._sock.recv(8192)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    break
                idle = 0
                text = buf.decode('utf-8', 'replace')
                if text.rstrip().endswith('ok') or '\nok' in text:
                    break
        except socket.timeout:
            idle += 1
            while idle < 2 and time.time() < deadline:
                try:
                    chunk = self._sock.recv(8192)
                except socket.timeout:
                    idle += 1
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                idle = 0
                if len(buf) > 65536:
                    break
                text = buf.decode('utf-8', 'replace')
                if text.rstrip().endswith('ok') or '\nok' in text:
                    break
        except OSError:
            pass
        self._sock.settimeout(self.timeout)
        return buf.decode('utf-8', 'replace')

    # -- what it can tell us ----------------------------------------------
    def info(self) -> dict:
        text = self.command('M115')
        out = {}
        for line in text.splitlines():
            if ':' not in line:
                continue
            key, _, value = line.partition(':')
            key = key.strip().lower()
            if key in ('machine type', 'machine name', 'firmware', 'sn',
                       'x', 'tool count'):
                out[key] = value.strip()
        out['raw'] = text.strip()
        return out

    def temperatures(self) -> dict:
        # The answer is one line: 'T0:26.1/0.0 T1:0.0/0.0 B:23.5/0.0'.
        text = self.command('M105')
        out = {}
        nozzle = re.search(r'T0:(-?[\d.]+)/(-?[\d.]+)', text)
        if nozzle:
            out['nozzle'] = float(nozzle.group(1))
            out['nozzle_target'] = float(nozzle.group(2))
        bed = re.search(r'(?:^|\s)B:(-?[\d.]+)/(-?[\d.]+)', text)
        if bed:
            out['bed'] = float(bed.group(1))
            out['bed_target'] = float(bed.group(2))
        return out

    def status(self) -> dict:
        text = self.command('M119')
        out = {"machine": "READY", "move": "", "file": "", "job": ""}
        for line in text.splitlines():
            if line.startswith('MachineStatus:'):
                out['machine'] = line.split(':', 1)[1].strip()
            elif line.startswith('MoveMode:'):
                out['move'] = line.split(':', 1)[1].strip()
            elif line.startswith('CurrentFile:'):
                out['file'] = line.split(':', 1)[1].strip()
        progress = self.command('M27')
        for line in progress.splitlines():
            if 'SD printing byte' in line:
                bits = line.split('byte', 1)[1].strip().split('/')
                try:
                    out['done'] = float(bits[0])
                    out['total'] = float(bits[1])
                    out['progress'] = (out['done'] / out['total']) if out['total'] else 0
                except (ValueError, IndexError):
                    pass
            if line.startswith('Layer:'):
                bits = line.split(':', 1)[1].strip().split('/')
                out['layer'] = bits[0].strip()
                out['layers'] = bits[1].strip() if len(bits) > 1 else ''
        return out

    def files(self) -> list:
        """The files on the printer's own storage."""
        text = self.command('M661', wait=0.8)
        out = []
        for part in text.split('::'):
            clean = ''.join(ch for ch in part if 32 <= ord(ch) < 127)
            if '/data/' in clean or '/user/' in clean:
                path = clean[clean.index('/'):].strip()
                if path and path not in out:
                    out.append(path)
        return out

    # -- what it can be told to do ----------------------------------------
    def select(self, path: str) -> str:
        return self.command('M23 ' + path, wait=0.8)

    def print_list(self) -> str:
        """What the printer is holding, over the plain channel."""
        return self.command('M661', wait=0.8)

    def start(self) -> str:
        return self.command('M24', wait=0.8)

    def pause(self) -> str:
        return self.command('M25', wait=0.8)

    def resume(self) -> str:
        return self.command('M24', wait=0.8)

    def stop(self) -> str:
        return self.command('M26', wait=0.8)


def http_post(host: str, path: str, body: dict, port: int = HTTP_PORT,
              timeout: float = 10.0) -> dict:
    """One call to the printer's JSON API."""
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request('http://%s:%d%s' % (host, port, path),
                                 data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as exc:
        return {"code": -1, "message": exc.read().decode('utf-8', 'replace')[:200]}
    except Exception as exc:
        return {"code": -1, "message": str(exc)[:200]}


def http_post_retry(host: str, path: str, body: dict, tries: int = 8,
                    pause: float = 1.2, port: int = HTTP_PORT,
                    timeout: float = 10.0) -> dict:
    """Call the printer again when it answers with its usual stutter.

    This firmware says 'Lan mode error' on roughly one call in six even
    when the serial number and the check code are both right. It is a flake
    in the printer's own LAN check, not a wrong code, so the honest thing to
    do is ask again. A wrong code keeps answering 'Access code is
    different' and is never papered over.
    """
    last = {"code": -1, "message": "no answer from the printer"}
    for attempt in range(tries):
        answer = http_post(host, path, body, port=port, timeout=timeout)
        if answer.get("code") == 0:
            return answer
        last = answer
        if answer.get("code") == 1:
            # The code itself is wrong - trying again will not help.
            break
        time.sleep(pause)
    return last


def printer_status_detail(host: str, serial: str, check_code: str,
                          port: int = HTTP_PORT) -> dict:
    """The printer's own account of what it is doing.

    The plain TCP channel only ever says BUSY, and that cannot tell a real
    print from the record of a job that was cancelled and never cleared.
    The HTTP API says 'ready', 'printing', 'pause' or 'cancel', and that
    difference decides whether a file can be sent at all.
    """
    answer = http_post_retry(host, '/detail',
                             {'serialNumber': serial, 'checkCode': check_code},
                             port=port)
    if answer.get("code") != 0:
        return {}
    detail = answer.get('detail') or {}

    def number(name, kind, fallback):
        try:
            return kind(detail.get(name))
        except (TypeError, ValueError):
            return fallback

    return {"state": str(detail.get("status") or ""),
            "file": str(detail.get("printFileName") or ""),
            "progress": number("printProgress", float, 0.0),
            "layers": number("targetPrintLayer", int, 0),
            "layer": number("printLayer", int, 0),
            "error": str(detail.get("errorCode") or "")}


def wait_until_ready(host: str, serial: str, check_code: str,
                     timeout: float = 25.0, port: int = HTTP_PORT) -> dict:
    """Wait for the printer to be ready for another file.

    This is what the printer's own manual asks for: when it says it is
    busy, keep asking until it says it is ready, then send the file. A
    cancelled job is the exception - that state does not clear itself, so
    there is no point in waiting for it.
    """
    deadline = time.time() + max(0.5, timeout)
    last = {}
    while True:
        last = printer_status_detail(host, serial, check_code, port=port)
        what = str(last.get("state") or "").lower()
        if what == "ready":
            return last
        if what in ("cancel", "cancelling", "canceling", "error"):
            # Nothing on the network clears these. Only the person at the
            # printer can, by turning it off and on again.
            return last
        if time.time() >= deadline:
            return last
        time.sleep(1.5)


def upload(host: str, serial: str, check_code: str, path: Path,
           print_now: bool = False, port: int = HTTP_PORT,
           timeout: float = 120.0, tries: int = 8,
           leveling: bool = True) -> dict:
    """Put a print file on the printer, over its own HTTP API.

    The file is the only thing this firmware will not take over the plain
    TCP channel - there it lands as an empty file. So the file goes up here,
    and the printer's own flakiness is answered with another try rather than
    with a failure the person has to understand.
    """
    data = path.read_bytes()
    boundary = '----MalmarsWorldGen'
    head = ('--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="gcodeFile"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n') % path.name
    tail = '\r\n--' + boundary + '--\r\n'
    body = head.encode('utf-8') + data + tail.encode('utf-8')
    headers = {
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
        'serialNumber': serial,
        'checkCode': check_code,
        'fileSize': str(len(data)),
        'printNow': 'true' if print_now else 'false',
        'levelingBeforePrint': 'true' if leveling else 'false',
        # The printer's own manual lists this whole set of headers for this
        # model. A firmware that finds one missing can answer 'Lan mode
        # error' instead of taking the file, so all of them are sent.
        'flowCalibration': 'false',
        'useMatlStation': 'false',
        'gcodeToolCnt': '0',
        'materialMappings': '[]',
    }
    last = {"code": -1, "message": "no answer from the printer"}
    # The printer refuses the file while it is still finishing the job
    # before it - it says "Printer is busy" for a while after a job
    # reports itself complete. The printer's own manual says to wait
    # until it says it is ready, and then send the file. So it is waited
    # for here, before the first attempt and after every refusal.
    wait_until_ready(host, serial, check_code, port=port)
    for attempt in range(tries):
        req = urllib.request.Request('http://%s:%d/uploadGcode' % (host, port),
                                     data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                answer = json.loads(resp.read().decode('utf-8', 'replace'))
        except urllib.error.HTTPError as exc:
            answer = {"code": -1,
                      "message": exc.read().decode('utf-8', 'replace')[:300]}
        except Exception as exc:
            answer = {"code": -1, "message": str(exc)[:200]}
        if answer.get("code") == 0:
            return answer
        last = answer
        code = answer.get("code")
        if code == 1:
            # The serial number or the access code is wrong. Trying again
            # will not help, and nothing else matters until that is right.
            break
        if code == 2:
            # The printer is busy. Its own manual says to keep asking until
            # it says it is ready, and then send the file again. It needs
            # a moment after saying "ready" before the upload path frees
            # up, so it is given one.
            wait_until_ready(host, serial, check_code, port=port)
            time.sleep(3.0)
            continue
        if code == -2:
            # This firmware says 'Lan mode error' on some calls even when
            # LAN mode is on and the access code is right. It is a stutter
            # in the printer's own network check, so the answer is another
            # try rather than a trip to the printer's settings.
            time.sleep(1.5)
            continue
        time.sleep(1.5)
    return last


def job_control(host: str, serial: str, check_code: str, action: str,
                port: int = HTTP_PORT) -> dict:
    """Pause, continue or cancel the job through the printer's own API.

    The plain TCP commands (M25, M26) are understood by every firmware,
    but a job that has gone wrong is only really cleared by the control
    API. It also refuses outright when the printer is not in LAN mode,
    which is worth saying out loud.
    """
    if action not in ("pause", "continue", "cancel"):
        return {"code": -1, "message": "unknown action"}
    body = json.dumps({"serialNumber": serial, "checkCode": check_code,
                       "payload": {"cmd": "jobCtl_cmd",
                                   "args": {"jobID": "", "action": action}}}).encode()
    req = urllib.request.Request('http://%s:%d/control' % (host, port), data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception as exc:
        return {"code": -1, "message": str(exc)[:200]}


def printer_files(host: str, serial: str, check_code: str,
                  port: int = HTTP_PORT) -> list:
    """The files the printer is holding, by its own account of them."""
    answer = http_post_retry(host, '/gcodeList',
                             {'serialNumber': serial, 'checkCode': check_code},
                             port=port)
    if answer.get("code") != 0:
        return []
    files = answer.get('gcodeList') or []
    return [str(name) for name in files]


def tool_wait(address: str) -> str:
    """What the printer's own display is asking, if anything.

    The Adventurer stops at its own screen and asks for the filament to
    be confirmed before it will start. That prompt shows up on the plain
    channel as WAIT_ON_TOOL. The app can see it, and it can answer it -
    the customer should never have to walk over to the machine.
    """
    try:
        with Printer(address) as link:
            status = link.status()
    except Exception:
        # The printer could not be reached. That is not the same as
        # "there is no question waiting", so it is kept apart.
        return "unknown"
    move = str((status or {}).get("move") or "").upper()
    machine = str((status or {}).get("machine") or "").upper()
    if "WAIT_ON_TOOL" in move or "WAIT" in move:
        return "filament"
    if machine in ("PAUSED", "PAUSE"):
        return "paused"
    return ""


def confirm_filament(address: str, serial: str = "", check_code: str = "",
                     port: int = HTTP_PORT) -> dict:
    """Answer the prompt on the printer's own screen, from the app.

    The printer's own control API is asked first, and then the plain
    channel's continue command, which every firmware understands. What
    was done is reported, and then the prompt is looked at again - a
    device that says it accepted a command is not the same as a
    question that has gone away, so the answer is checked before it is
    believed.
    """
    tried = []
    answer = job_control(address, serial, check_code, "continue", port=port)
    tried.append({"how": "the printer's own control", "code": answer.get("code"),
                  "message": str(answer.get("message") or "")[:120]})
    try:
        with Printer(address) as link:
            link.resume()
        tried.append({"how": "the plain channel", "code": 0,
                      "message": "continue sent"})
    except Exception as exc:
        tried.append({"how": "the plain channel", "code": -1,
                      "message": str(exc)[:120]})
    time.sleep(2.0)
    left = tool_wait(address)
    reached = left != "unknown"
    return {"ok": reached and not left, "prompt": left, "reached": reached,
            "tried": tried}
