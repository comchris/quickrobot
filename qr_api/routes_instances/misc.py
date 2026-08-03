"""Misc endpoint handlers for quickrobot instances API.

Includes model SSE proxy, remote proxy, system restart, and iperf3 helpers.
Functions are registered with routes in __init__.py via app.add_url_rule().
"""

import json
import logging
from flask import request, Response

from qr_api.lib_responses import success_single, error_response
from qr_api import _CONFIG
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_PORT_DEFAULTS, \
    QR_ENGINE_LLAMA_SERVER_NAME, QR_STATE_LOADING
from db.adapters.instances import get_instance as _gi, check_system_managed as _csm
from db.adapters.nodes import get_node as _gn

logger = logging.getLogger(__name__)


def api_model_load_sse(inst_id):
    """SSE proxy: stream /models/sse from remote llama_server instance.

    Connects to the remote llama-server's /models/sse endpoint and streams
    SSE events back to the client through this API endpoint. Provides model
    loading progress (stage + percentage) for WebUI progress bars.

    Only works for llama_server engine type. Returns 404 if the remote
    server does not support /models/sse (old llama.cpp version).

    Args:
        inst_id: Instance ID to proxy SSE from.
    """
    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
        return error_response("INVALID_ENGINE", "SSE model load only works for llama_server instances")

    # Get remote host info
    node_id = inst.get("node_id")
    nd = _gn(_CONFIG["db_path"], node_id) if node_id else None
    hostname = (nd.get("ansible_inventory_host") or nd.get("hostname")) if nd else None
    port = inst.get("port_assigned")
    if not hostname or not port:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} host/port info missing")

    sse_url = f"http://{hostname}:{port}/models/sse"

    def _transition_from_loading(conn, inst_id):
        """Transition instance from 'loading' to 'running' when SSE detects completion."""
        try:
            cur = conn.execute("SELECT state FROM instances WHERE id=?", (inst_id,)).fetchone()
            if cur and cur["state"] == QR_STATE_LOADING:
                conn.execute(
                    "UPDATE instances SET state='running', last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (inst_id,),
                )
        except Exception as _e:
            logger.debug("api_model_load_sse inst=%d: running state transition via SSE proxy: %s", inst_id, _e)
            pass  # Non-critical — SSE streaming must continue regardless

    def generate():
        import json as _json
        import requests as _requests
        from db.sqlite import pool
        try:
            resp = _requests.get(sse_url, stream=True, timeout=300, headers={"Accept": "text/event-stream"})
            for line in resp.iter_lines(decode_unicode=True):
                if line:
                    # Check for model load completion events (status field in SSE data)
                    try:
                        ev = _json.loads(line)
                        if isinstance(ev, dict) and ev.get("status") in ("loaded", "sleeping"):
                            with pool(_CONFIG["db_path"]) as pconn:
                                _transition_from_loading(pconn, inst_id)
                    except Exception as _e:
                        logger.debug("api_model_load_sse inst=%d: transition_from_loading call: %s", inst_id, _e)
                        pass  # Not JSON or no status field — stream normally
                    yield line + "\n"
                else:
                    yield "\n"  # SSE blank line separator
        except _requests.ConnectionError as e:
            yield f"data: {{\"error\": \"Cannot connect to {hostname}:{port}: {e}\"}}\n\n"
        except Exception as e:
            yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"
        finally:
            # Fallback: if SSE connection ended (404, timeout, error), transition loading→running.
            with pool(_CONFIG["db_path"]) as pconn:
                _transition_from_loading(pconn, inst_id)

    return Response(generate(), mimetype='text/event-stream')


def api_proxy_remote(subpath):
    """Reverse proxy to a remote instance's web UI.

    Forwards requests to the remote instance (identified by node + port).
    CORS headers added by global flask-cors middleware.

    Args:
        subpath: Instance ID followed by path, e.g., '123/health' or '123/'.

    Returns:
        Proxied response (CORS handled by middleware).
    """
    import urllib.request as _urq
    import urllib.error as _ure

    # Parse instance ID and target path from subpath
    parts = subpath.split("/", 1)
    if len(parts) < 2:
        return Response('{"status":"error","code":"BAD_REQUEST","message":"Usage: /api/v1/proxy/<instance_id>/<path>"}',
                            status=400, content_type="application/json; charset=utf-8")

    inst_id = int(parts[0])
    target_path = parts[1] or "/"

    # Verify instance exists and get its node + port
    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    node_hostname = inst.get("node_hostname") or inst.get("ipv4_address", QR_DEFAULT_LOCALHOST)
    engine_type = inst.get("engine_type_name", "")
    default_port = QR_ENGINE_PORT_DEFAULTS.get(engine_type, QR_ENGINE_PORT_DEFAULTS[QR_ENGINE_LLAMA_SERVER_NAME])
    port = inst.get("port_assigned") or default_port

    # Build target URL — avoid double slashes
    if target_path == "":
        base_url = f"http://{node_hostname}:{port}/"
    elif target_path.startswith("/"):
        base_url = f"http://{node_hostname}:{port}{target_path}"
    else:
        base_url = f"http://{node_hostname}:{port}/{target_path}"
    if request.query_string:
        base_url += "?" + request.query_string.decode()

    # Forward the request — set Host to target, forward other headers
    headers = {"Host": f"{node_hostname}:{port}"}
    for key, value in request.headers:
        if key.lower() not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value

    try:
        data = request.get_data() if request.method in ("POST", "PUT") else None
        from lib.lib_proxy_reader import proxy_request as _proxy_req
        body, status_code, resp_headers = _proxy_req(
            base_url, data=data, headers=headers,
            method=request.method, timeout=60)

        # Clean proxied response headers — CORS added by middleware
        clean_headers = []
        for k, v in resp_headers.items():
            kl = k.lower()
            if kl == "content-type":
                clean_headers.append((k, f"{v}; charset=utf-8"))
            elif kl == "content-length":
                continue  # let Flask set it
            else:
                clean_headers.append((k, v))

        return Response(body, status=status_code, headers=dict(clean_headers))

    except _ure.HTTPError as e:
        body = e.read()
        resp_headers = list(e.headers.items()) if hasattr(e, 'headers') else []
        clean_headers = []
        for k, v in resp_headers:
            kl = k.lower()
            if kl == "content-type":
                clean_headers.append((k, f"{v}; charset=utf-8"))
            elif kl == "content-length":
                continue
            else:
                clean_headers.append((k, v))
        return Response(body, status=e.code, headers=dict(clean_headers))

    except Exception as exc:
        # Handle ProxyConnectionError with its descriptive message
        from lib.lib_proxy_reader import ProxyConnectionError as _PCE
        if isinstance(exc, _PCE):
            error_msg = str(exc)
        else:
            error_msg = f"Proxy error: {exc}"
        error_body = f'{{"status":"error","code":"PROXY_ERROR","message":"{error_msg}"}}'.encode()
        return Response(error_body, status=502, content_type="application/json; charset=utf-8")


def api_restart_system_instance(inst_id):
    """Restart a system-managed instance.

    Unified restart handler: delegates to api_restart_instance which auto-detects
    system-managed instances and routes to the correct subprocess path.
    This consolidates the restart logic into one endpoint instead of having
    separate /instances/<id>/restart and /instances/<id>/restart_system endpoints.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        Action result dict with status message.
    """
    from qr_api.routes_instances import api_restart_instance as _restart
    return _restart(inst_id)


def _run_iperf3_client(inst_id, engine_type_name, node_id, inv_hostname):
    """Run an iperf3 client to completion and return benchmark results.

    Starts the client service via systemctl, polls until it exits (one-shot),
    then fetches the log file and parses throughput results.

    Args:
        inst_id: Integer instance ID.
        engine_type_name: Engine type string ("iperf3").
        node_id: Target node ID.
        inv_hostname: Resolved hostname for the target node.

    Returns:
        success_single dict with action, log content, parsed results, or error_response.
    """
    from db.adapters.instances import get_instance as _gi2, transition_state, \
        log_action
    from lib.lib_ansible_runner import run_playbook
    from lib.lib_constants import DEFAULT_ANSIBLE_USER

    try:
        # Start the client service (one-shot execution)
        from qr_api.lib_instances import _run_manage_action
        start_result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
        if not start_result.get("success"):
            return error_response("START_FAILED",
                                f"Client start failed: {start_result.get('error', 'unknown')}")

        log_action(_CONFIG["db_path"], inst_id, "client_run", "started")

        # Transition to starting state while running
        try:
            transition_state(_CONFIG["db_path"], inst_id, "starting")
        except Exception as _e:
            logger.debug("_run_iperf3_client inst=%d: starting state transition for client: %s", inst_id, _e)
            pass

        # Poll until the service exits (one-shot run)
        import time as _time
        start_wait = _time.time()
        max_wait = 300  # 5 minutes max
        client_log = ""
        exit_ok = False

        while _time.time() - start_wait < max_wait:
            _time.sleep(3)
            try:
                status_result = _run_manage_action(inst_id, engine_type_name, node_id, "status")
                if not isinstance(status_result, dict):
                    status_result = {"failed": True, "error": str(status_result)}

                # Check if the service is still active
                plays = status_result.get("results", {}).get("plays", [])
                is_active = False
                for play in plays:
                    for task in play.get("tasks", []):
                        tname = task.get("task", {}).get("name", "")
                        for entry in task.get("results", []):
                            if "Get service status" in tname:
                                stdout_val = entry.get("stdout", "").strip()
                                if stdout_val == "active":
                                    is_active = True
                if not is_active:
                    exit_ok = True
                    break

            except Exception as _e:
                logger.debug("_run_iperf3_client inst=%d: status poll iteration failed, retrying: %s", inst_id, _e)
                continue  # Poll failure, try again

        if not exit_ok:
            # Timeout — stop and log
            _run_manage_action(inst_id, engine_type_name, node_id, "stop")
            return error_response("TIMEOUT",
                                f"Client run exceeded {max_wait}s timeout")

        # Fetch the log file content from remote node
        import subprocess as _sub
        log_path = f"/var/log/qr/iperf3-{inst_id}.log"
        ssh_cmd = (
            f'ssh -o ConnectTimeout=10 {DEFAULT_ANSIBLE_USER}@{inv_hostname} '
            f"'tail -100 {log_path} 2>/dev/null || echo \"(no log found)\"'"
        )
        try:
            log_proc = _sub.run(ssh_cmd, capture_output=True, text=True, timeout=15)
            client_log = (log_proc.stdout or "(no log available)").strip()
        except Exception as _e:
            logger.debug("_run_iperf3_client inst=%d: log retrieval failed: %s", inst_id, _e)
            client_log = "(unable to retrieve log)"

        # Parse iperf3 log output for throughput results
        parsed = _parse_iperf3_log(client_log)

        # Transition to deployed (client ran once and finished)
        try:
            transition_state(_CONFIG["db_path"], inst_id, "deployed")
        except Exception as _e:
            logger.debug("_run_iperf3_client inst=%d: deployed state transition after client run: %s", inst_id, _e)

        log_action(_CONFIG["db_path"], inst_id, "client_run", "success",
                    detail={"sent_mbits": parsed.get("sent_mbits"),
                            "received_mbits": parsed.get("received_mbits")})

        return success_single({
            "action": "run_client",
            "instance_id": inst_id,
            "success": True,
            "log_file": f"/var/log/qr/iperf3-{inst_id}.log",
            "log_excerpt": client_log[:2000],
            "parsed_results": parsed,
        })

    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, "client_run", "failed",
                    detail={"error": str(exc)})
        return error_response("CLIENT_RUN_ERROR", str(exc))


def _parse_iperf3_log(log_text):
    """Parse iperf3 output for throughput results.

    Args:
        log_text: Raw iperf3 log output string.

    Returns:
        dict with keys: sent_mbits, received_mbits, duration_seconds,
            sender_loss_pct, receiver_loss_pct. All numeric values are None
            if not found in the log.
    """
    import re as _re
    result = {
        "sent_mbits": None,
        "received_mbits": None,
        "duration_seconds": None,
        "sender_loss_pct": None,
        "receiver_loss_pct": None,
    }

    if not log_text or "iperf" not in log_text.lower():
        return result

    # Match summary lines like: "[  5]   0.00-40.28  sec  75.3 GBytes  16.1 Gbits/sec    0            sender"
    pattern = (
        r'\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+'
        r'([\d.]+)\s+(Bytes|KBytes|MBytes|GBytes|TBytes)\s+'
        r'([\d.]+)\s+(bits/sec|Kbits/sec|Mbits/sec|Gbits/sec|Tbits/sec)'
    )
    matches = list(_re.finditer(pattern, log_text))
    if matches:
        last = matches[-1]
        # Duration from last interval
        try:
            result["duration_seconds"] = float(last.group(2)) - float(last.group(1))
        except (ValueError, TypeError):
            pass
        # Bandwidth from last interval
        bw_val = float(last.group(5))
        bw_unit = last.group(6).lower()
        if "tbits" in bw_unit:
            result["sent_mbits"] = bw_val * 1000000
        elif "gbits" in bw_unit:
            result["sent_mbits"] = bw_val * 1000
        elif "mbits" in bw_unit:
            result["sent_mbits"] = bw_val
        elif "kbits" in bw_unit:
            result["sent_mbits"] = bw_val / 1000

    # Find receiver line (contains both sender and receiver in summary)
    recv_pattern = (
        r'\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+'
        r'([\d.]+)\s+(Bytes|KBytes|MBytes|GBytes|TBytes)\s+'
        r'([\d.]+)\s+(bits/sec|Kbits/sec|Mbits/sec|Gbits/sec|Tbits/sec)\s+\d+\s+\S+\s+receiver'
    )
    recv_match = _re.search(recv_pattern, log_text, _re.IGNORECASE)
    if recv_match:
        bw_val = float(recv_match.group(5))
        bw_unit = recv_match.group(6).lower()
        if "tbits" in bw_unit:
            result["received_mbits"] = bw_val * 1000000
        elif "gbits" in bw_unit:
            result["received_mbits"] = bw_val * 1000
        elif "mbits" in bw_unit:
            result["received_mbits"] = bw_val
        elif "kbits" in bw_unit:
            result["received_mbits"] = bw_val / 1000

    # Find loss percentage: e.g., "0.00% loss"
    loss_match = _re.search(r'([\d.]+)%\s+loss', log_text)
    if loss_match:
        result["receiver_loss_pct"] = float(loss_match.group(1))

    return result
