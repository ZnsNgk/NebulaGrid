import paramiko
import time
import json
import traceback
from datetime import datetime
import threading
import sys
from math import log
from queue import Queue
from config import config
from logger import log as write_log


class Monitor:
    def __init__(self, config: config, slaver_queue: Queue, online_slaver: list, slaver_state) -> None:
        # -u is important: get_info.py may use buffered output when executed through SSH.
        self.py_cmd = "source ~/envs/miniconda3/bin/activate&&export CUDA_DEVICE_ORDER=\"PCI_BUS_ID\"&&python -u ~/envs/get_info.py -l 1"
        self.config = config
        self.psw = self.config.psw
        self.servers = []
        self.slaver_info = self.config.slaver_info
        self.slaver_queue = slaver_queue
        self.online_slaver = online_slaver
        self.slaver_state = slaver_state

        # Web端读取的最新节点监控快照。socket客户端仍按原逻辑接收mon消息。
        self.latest_info = {}
        self.latest_lock = threading.Lock()

        # Runtime reconnect control.
        # A node can look online forever if the old SSH monitor session hangs after
        # abnormal power-off. Admin "reconnect" closes that stale SSH session and
        # asks fetch_loop to reconnect immediately.
        self.comm = None
        self.session_lock = threading.RLock()
        self.node_sessions = {}       # nickname -> {client, stdout, stderr, channel, connected_at}
        self.reconnect_requested = set()
        self.last_reconnect_at = {}   # nickname -> iso timestamp
        # Nodes deleted from admin backend. Their monitor threads should exit and
        # must not reconnect until the node is added again. This is not used for
        # one-shot force-offline.
        self.deleted_nodes = set()
        # Nodes intentionally disconnected by admin.  They stay disconnected until
        # admin clicks "重新连接节点".  This is different from the old guard loop:
        # it does not continuously remove a node that has already reconnected by
        # itself; instead fetch_loop simply waits here and reconnects only when
        # requested.
        self.manual_offline_nodes = set()
        self.last_force_offline_at = {}  # nickname -> iso timestamp

        for slaver in self.slaver_info:
            info = (slaver["ip"], slaver["name"], 22, slaver["user_name"], self.py_cmd)
            self.servers.append(info)
        self.is_running = True
        self.byteunits = ('B', 'KB', 'MB', 'GB', 'TB')
        self.webbyteunits = ('bps', ' Kbps', 'Mbps', 'Gbps')
        self.slaver_lock = threading.Lock()
        self.use_influx = False
        try:
            from influxdb import InfluxDBClient
            self.dbclient = InfluxDBClient('localhost', 8086, 'python', 'python', 'cvlab')
            self.use_influx = True
            write_log("节点信息数据库连接成功", self.config.config["server_log_path"])
        except Exception as e:
            write_log("节点信息数据库连接失败", self.config.config["server_log_path"])
            print(e)

    # ------------------------------------------------------------------
    # Web UI / runtime helpers
    # ------------------------------------------------------------------
    def update_latest_info(self, nickname, info):
        """Store the latest monitor snapshot for the embedded Web UI."""
        try:
            self.latest_lock.acquire()
            self.latest_info[nickname] = info
        finally:
            self.latest_lock.release()

    def mirror_monitor_packet(self, payload):
        """Best-effort mirror for packets sent through monitor_comm.send_all."""
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
            if data.get("mode") == "mon" and isinstance(data.get("info"), dict):
                info = data["info"]
                name = info.get("name")
                if name:
                    self.update_latest_info(name, info)
        except Exception:
            pass

    def _offline_info(self, nickname, reason="No data"):
        return {
            "name": nickname,
            "cpu_usage": "No data",
            "avail_ram": "No data",
            "web_up": "No data",
            "web_down": "No data",
            "gpus": [{"gpu_used": "No data", "avail_vram": "No data", "mem_util": 0}],
            "online": self.online_slaver,
            "cpu_used": 0,
            "gpu_used": [],
            "reconnect_reason": reason,
        }

    def _remove_node_runtime_state(self, nickname):
        """Remove a node from online/scheduler state and purge stale queue entries."""
        try:
            while nickname in self.online_slaver:
                self.online_slaver.remove(nickname)
        except Exception:
            pass
        try:
            self.slaver_state[:] = [x for x in self.slaver_state if x.get("name") != nickname]
        except Exception:
            pass
        # The task scheduler consumes slaver_queue asynchronously. If old states are
        # left in the queue, a supposedly reconnected/offline node may briefly revive.
        try:
            kept = []
            while not self.slaver_queue.empty():
                item = self.slaver_queue.get_nowait()
                if not (isinstance(item, dict) and item.get("name") == nickname):
                    kept.append(item)
            for item in kept:
                if not self.slaver_queue.full():
                    self.slaver_queue.put_nowait(item)
        except Exception:
            pass

    def broadcast_offline(self, nickname, reason="manual reconnect requested", comm=None):
        """Broadcast a no-data monitor packet, matching the normal offline path."""
        self._remove_node_runtime_state(nickname)
        offline_info = self._offline_info(nickname, reason=reason)
        self.update_latest_info(nickname, offline_info)
        comm = comm or self.comm
        if comm is not None:
            try:
                comm.send_all(json.dumps({"mode": "mon", "ui": "main", "info": offline_info}))
            except Exception:
                pass
        return offline_info

    def _close_node_session(self, nickname):
        """Close the current Paramiko monitor session for a node, if any."""
        closed = False
        with self.session_lock:
            session = self.node_sessions.get(nickname)
            if session:
                for key in ("channel",):
                    obj = session.get(key)
                    if obj is not None:
                        try:
                            obj.close()
                            closed = True
                        except Exception:
                            pass
                for key in ("stdout", "stderr"):
                    obj = session.get(key)
                    if obj is not None:
                        try:
                            obj.close()
                        except Exception:
                            pass
                client = session.get("client")
                if client is not None:
                    try:
                        client.close()
                        closed = True
                    except Exception:
                        pass
        return closed

    def clear_latest_info(self, nickname):
        try:
            self.latest_lock.acquire()
            self.latest_info.pop(nickname, None)
        finally:
            self.latest_lock.release()

    def disconnect_node(self, nickname, reason="admin force offline", stay_offline=True):
        """Disconnect master from a node.

        When stay_offline=True, this is the admin "强制下线" primitive: close the
        current monitor SSH session, clear online/scheduler state, broadcast No data,
        and keep fetch_loop waiting until request_reconnect_node() is called.  Tasks
        should be handled by task_ctrl.force_node_offline() in the admin route.

        When stay_offline=False, it behaves as a one-shot disconnect and the normal
        monitor loop may reconnect later.
        """
        nickname = (nickname or "").strip()
        if not nickname:
            raise ValueError("缺少节点名称")
        now = datetime.now().isoformat(timespec="seconds")
        with self.session_lock:
            self.last_force_offline_at[nickname] = now
            if stay_offline:
                self.manual_offline_nodes.add(nickname)
                self.reconnect_requested.discard(nickname)
        closed = self._close_node_session(nickname)
        self.broadcast_offline(nickname, reason=reason)
        try:
            write_log("Web管理员强制下线节点 " + nickname + "，已断开master侧监控连接" + ("，等待手动重新连接" if stay_offline else ""), self.config.config["server_log_path"])
        except Exception:
            pass
        return {
            "name": nickname,
            "closed_monitor_session": closed,
            "manual_offline": bool(stay_offline),
            "suppressed": False,
            "note": "已断开master与该节点的当前监控连接并清理在线状态；节点将保持手动下线，直到点击“重新连接节点”。" if stay_offline else "已断开master与该节点的当前监控连接并清理在线状态；不会后台压制该节点。"
        }

    def remove_node(self, nickname, reason="admin delete node"):
        """Remove a node completely from monitor runtime state.

        Used when the node is deleted from gpu_slaver_info.json. Existing monitor
        thread for this node exits, old sessions are closed, queues/cache are
        purged, and self.servers/slaver_info no longer contain this node.
        """
        nickname = (nickname or "").strip()
        if not nickname:
            raise ValueError("缺少节点名称")
        with self.session_lock:
            self.deleted_nodes.add(nickname)
            self.reconnect_requested.discard(nickname)
            self.manual_offline_nodes.discard(nickname)
        closed = self._close_node_session(nickname)
        self._remove_node_runtime_state(nickname)
        self.clear_latest_info(nickname)
        try:
            self.servers[:] = [x for x in self.servers if x[1] != nickname]
        except Exception:
            pass
        try:
            self.slaver_info[:] = [x for x in self.slaver_info if x.get("name") != nickname]
        except Exception:
            pass
        try:
            write_log("Web管理员删除节点 " + nickname + "，已从monitor运行态清除", self.config.config["server_log_path"])
        except Exception:
            pass
        return {"name": nickname, "closed_monitor_session": closed, "removed_from_monitor": True, "note": "已从monitor连接、缓存、队列和服务器列表中清除该节点；对应监控线程会退出。"}

    def enable_node(self, nickname):
        """Allow a previously deleted node name to be monitored again."""
        nickname = (nickname or "").strip()
        with self.session_lock:
            self.deleted_nodes.discard(nickname)
            self.manual_offline_nodes.discard(nickname)
        return {"name": nickname, "enabled": True}

    def is_node_deleted(self, nickname):
        with self.session_lock:
            return nickname in self.deleted_nodes

    def is_node_manual_offline(self, nickname):
        with self.session_lock:
            return nickname in self.manual_offline_nodes

    def request_reconnect_node(self, nickname, reason="admin reconnect"):
        """Force a node monitor session to reconnect.

        This is intentionally different from "blocking" a node. It closes the stale
        Paramiko channel/client if present, removes runtime online/scheduler state,
        broadcasts an offline packet, and lets fetch_loop reconnect in ~1 second.
        """
        nickname = (nickname or "").strip()
        if not nickname:
            raise ValueError("缺少节点名称")

        with self.session_lock:
            self.deleted_nodes.discard(nickname)
            self.manual_offline_nodes.discard(nickname)
            self.reconnect_requested.add(nickname)
            self.last_reconnect_at[nickname] = datetime.now().isoformat(timespec="seconds")
        closed = self._close_node_session(nickname)
        self.broadcast_offline(nickname, reason=reason)
        try:
            write_log("Web管理员请求节点 " + nickname + " 断开旧监控连接并重新连接", self.config.config["server_log_path"])
        except Exception:
            pass
        return {"name": nickname, "closed_old_session": closed, "reconnect_requested": True, "manual_offline_cleared": True, "note": "已解除手动下线状态，清理旧在线状态并关闭旧SSH监控会话；monitor线程会立即重连。"}

    def pop_reconnect_requested(self, nickname):
        with self.session_lock:
            if nickname in self.reconnect_requested:
                self.reconnect_requested.discard(nickname)
                return True
        return False

    def register_session(self, nickname, client, stdout=None, stderr=None):
        try:
            channel = stdout.channel if stdout is not None else None
        except Exception:
            channel = None
        with self.session_lock:
            self.node_sessions[nickname] = {
                "client": client,
                "stdout": stdout,
                "stderr": stderr,
                "channel": channel,
                "connected_at": datetime.now().isoformat(timespec="seconds"),
            }

    def unregister_session(self, nickname, client=None):
        with self.session_lock:
            old = self.node_sessions.get(nickname)
            if old is None:
                return
            if client is None or old.get("client") is client:
                self.node_sessions.pop(nickname, None)


    def wait_manual_reconnect(self, nickname):
        """Wait while a node is manually offline, until admin requests reconnect."""
        while self.is_running:
            with self.session_lock:
                if nickname in self.deleted_nodes:
                    return False
                if nickname not in self.manual_offline_nodes:
                    return True
                if nickname in self.reconnect_requested:
                    self.manual_offline_nodes.discard(nickname)
                    return True
            time.sleep(0.5)
        return False

    def wait_retry_delay(self, nickname, delay):
        """Sleep before reconnect, but wake early when admin requests reconnect."""
        end = time.time() + max(0, delay)
        while self.is_running and time.time() < end:
            with self.session_lock:
                if nickname in self.reconnect_requested or nickname in self.manual_offline_nodes:
                    return
            time.sleep(min(0.5, max(0, end - time.time())))

    def fetch_hw_info(self, server, nickname, port, username, cmd, comm):

        def parse_info_to_json(r):
            info = json.loads(r)
            ts = datetime.utcnow().isoformat()

            def get_common_body(measurement, name, fields):
                return {
                    "measurement": measurement,
                    "tags": {"host": nickname, measurement: name},
                    "time": ts,
                    "fields": fields,
                }

            cpu_body = [get_common_body("cpu", f"cpu{i:d}", {"value": j}) for i, j in enumerate(info['cpu'])]
            cpu_body.append(get_common_body("cpu", "cpu-total", {"value": info['cpu_total']}))
            ram_body = [{"measurement": "ram", "tags": {"host": nickname}, "time": ts, "fields": info['ram']}]

            def parse_gpu(js):
                js['mem_available'] = js['mem_total'] - js['mem_used']
                del js['id']
                return js

            def parse_net(js):
                js['recv_bytes_ps'] = float(js['recv_bytes_ps'])
                js['sent_bytes_ps'] = float(js['sent_bytes_ps'])
                del js['id']
                return js

            net_body = [get_common_body("net", net['id'], parse_net(net)) for net in info['net']]
            if 'gpu' in info:
                gpu_body = [get_common_body("gpu", f"gpu{j['id']}", parse_gpu(j)) for j in info['gpu']]
            else:
                gpu_body = []
            return cpu_body + ram_body + gpu_body + net_body

        def format_json(r, nickname):
            r = json.loads(r)

            def filesizeformat(value, byteunits):
                exponent = int(log(value + 0.1, 1024))
                exponent = max(0, min(exponent, len(byteunits) - 1))
                return "%.1f %s" % (float(value) / pow(1024, exponent), byteunits[exponent])

            gpus = []
            try:
                if len(r["gpu"]) == 0:
                    gpus = [{"gpu_used": "No data", "avail_vram": "No data", "vram_percent": "No data", "mem_util": 0}]
                else:
                    for gpu in r["gpu"]:
                        gpu_info = {
                            "gpu_used": str(gpu["load"] * 100) + " %",
                            "avail_vram": str(round((gpu["mem_total"] - gpu["mem_used"]) / 1024., 2)) + " GB",
                            "mem_util": gpu["mem_util"],
                        }
                        gpus.append(gpu_info)
            except Exception:
                gpus = [{"gpu_used": "No data", "avail_vram": "No data", "vram_percent": "No data", "mem_util": 0}]

            cpu_used = 0
            gpu_used = []
            for state in self.slaver_state:
                if nickname == state["name"]:
                    cpu_used = int(state["cpu_used"])
                    for g in state["gpu_used"]:
                        gpu_used.append(int(g))
                    break
            try:
                info = {
                    "name": nickname,
                    "cpu_usage": str(r["cpu_total"]) + " %",
                    "avail_ram": filesizeformat(r["ram"]["available"], self.byteunits),
                    "web_up": filesizeformat(r["net"][0]["sent_bytes_ps"], self.webbyteunits),
                    "web_down": filesizeformat(r["net"][0]["recv_bytes_ps"], self.webbyteunits),
                    "gpus": gpus,
                    "online": self.online_slaver,
                    "cpu_used": cpu_used,
                    "gpu_used": gpu_used,
                }
            except Exception:
                info = {
                    "name": nickname,
                    "cpu_usage": str(r["cpu_total"]) + " %",
                    "avail_ram": filesizeformat(r["ram"]["available"], self.byteunits),
                    "web_up": "No data",
                    "web_down": "No data",
                    "gpus": gpus,
                    "online": self.online_slaver,
                    "cpu_used": cpu_used,
                    "gpu_used": gpu_used,
                }
            data = {"mode": "mon", "ui": "main", "info": info}
            self.update_latest_info(nickname, info)
            if self.slaver_queue.full():
                self.slaver_queue.get()
            self.slaver_queue.put(info)
            return json.dumps(data)

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(server, username=username, port=port, timeout=30, password=self.psw)
            stdin, stdout, stderr = client.exec_command(cmd)
            self.register_session(nickname, client, stdout, stderr)
            write_log("计算节点 " + str(nickname) + " 连接成功", self.config.config["server_log_path"])
            while self.is_running:
                try:
                    if nickname not in self.online_slaver:
                        self.online_slaver.append(nickname)
                    r = stdout.readline()
                    if len(r):
                        send_info = format_json(r, nickname)
                        comm.send_all(send_info)
                        if self.use_influx:
                            try:
                                if self.dbclient is not None:
                                    points = parse_info_to_json(r)
                                    self.dbclient.write_points(points, time_precision='ms')
                            except Exception:
                                pass
                    if stdout.channel.exit_status_ready():
                        break
                except Exception:
                    print(f"Server: {nickname}:")
                    traceback.print_exc()
                    print('*' * 8)
                    break
        except Exception as e:
            if nickname in self.online_slaver:
                self.online_slaver.remove(nickname)
                write_log("计算节点 " + nickname + " 连接错误", self.config.config["server_log_path"])
            print(f"Connection to {nickname} failed")
        finally:
            self.unregister_session(nickname, client)
            try:
                client.close()
            except Exception:
                pass
            if nickname in self.online_slaver:
                try:
                    self.online_slaver.remove(nickname)
                except Exception:
                    pass
                write_log("计算节点 " + nickname + " 掉线", self.config.config["server_log_path"])
                self.broadcast_offline(nickname, reason="monitor disconnected", comm=comm)

    def fetch_loop(self, *args):
        retry_delay = 30
        nickname = args[1]
        while self.is_running:
            if self.is_node_deleted(nickname):
                break
            if self.is_node_manual_offline(nickname):
                try:
                    write_log("节点 " + nickname + " 处于管理员手动下线状态，等待重新连接", self.config.config["server_log_path"])
                except Exception:
                    pass
                if not self.wait_manual_reconnect(nickname):
                    break
                continue
            pending_before_connect = self.pop_reconnect_requested(nickname)
            if self.is_node_deleted(nickname):
                break
            if self.is_node_manual_offline(nickname):
                continue
            self.fetch_hw_info(*args)
            if self.is_running:
                if self.is_node_deleted(nickname):
                    break
                if self.is_node_manual_offline(nickname):
                    continue
                requested = self.pop_reconnect_requested(nickname) or pending_before_connect
                delay = 1 if requested else retry_delay
                print(f"[{args[2]}]: Found some error, try again after {delay}s", file=sys.stderr)
                self.wait_retry_delay(nickname, delay)
        try:
            write_log("节点 "+nickname+" 的monitor线程已退出", self.config.config["server_log_path"])
        except Exception:
            pass

    def run(self, comm):
        self.comm = comm
        thres = [threading.Thread(target=self.fetch_loop, args=(*server_info, comm)) for server_info in self.servers]
        for i, j in enumerate(self.servers):
            print(f"Connect to {j[1]}")
            thres[i].start()
        try:
            while True:
                time.sleep(60)
                try:
                    self.config.update_slaver_info()
                    for server in self.slaver_info:
                        name = server["name"]
                        flag = True
                        for s in self.servers:
                            if s[1] == name:
                                flag = False
                                break
                        if flag and not self.is_node_deleted(name) and not self.is_node_manual_offline(name):
                            info = (server["ip"], server["name"], 22, server["user_name"], self.py_cmd)
                            print(info)
                            self.servers.append(info)
                            thres.append(threading.Thread(target=self.fetch_loop, args=(*info, comm)))
                            thres[-1].start()
                except Exception as e:
                    print("服务器列表更新失败, 原因是: " + str(e))
        except Exception:
            pass
        self.is_running = False
        print("Stop all threads")
        for i, j in enumerate(self.servers):
            print(f"Disconnect from {j[1]}")
            thres[i].join()
        print("All threads done")


if __name__ == "__main__":
    mon = Monitor()
    mon.run()
