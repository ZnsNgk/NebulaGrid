"""从受限关键词和计数结构提取进度；日志仅作数据，不执行日志中的任何内容。"""

import codecs
import hashlib
import math
import re
from statistics import median

# 修复解析规则时递增版本，扫描器会重建旧摘要，避免沿用已污染的轮次/速度。
PARSER_VERSION = 2
# 扩展单位只需维护别名表，无需为 Episode/Task 等另写整套状态判断。
UNITS = {
    "epoch": ("epoch",), "step": ("step", "iter", "iteration", "batch"),
    "episode": ("episode",), "task": ("task",), "trial": ("trial",),
    "round": ("round",), "fold": ("fold",),
}
ALIASES = {alias: name for name, aliases in UNITS.items() for alias in aliases}
WORDS = "|".join(sorted(ALIASES, key=len, reverse=True))
LABELS = {unit: unit.title() for unit in UNITS}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
COUNT = r"(\d+(?:\.\d+)?[kKmMgG]?)"
FRACTION = re.compile(COUNT + r"\s*/\s*" + COUNT)
NAMED = re.compile(rf"(?<![\w])(?P<unit>{WORDS})s?\s*[:=]?\s*\[?\s*(?P<n>\d+)(?:\s*/\s*(?P<total>\d+))?", re.I)
TOTAL = re.compile(
    rf"(?<![\w])(?:[\"']?(?P<prefix>total|max|num|n|training)[_ ](?:train[_ ])?)?"
    rf"[\"']?(?P<unit>{WORDS})(?P<plural>s)?[\"']?\s*[:=]\s*(?P<n>\d+)\b(?![.\d])", re.I)
PHASE = re.compile(r"(?:^|[\s\[:])(?P<name>sanity checking|validation|valid|eval|test|training|train|val)\b(?=\s*(?:[:\]|]|dataloader|\d|$))", re.I)
PHASES = {"train": "训练", "training": "训练", "val": "验证", "valid": "验证",
          "validation": "验证", "eval": "评估", "test": "测试", "sanity checking": "训练前检查"}
CLOCK = r"(?:(?:\d+)\s+days?,\s*)?\d+:\d{2}(?::\d{2})?"
METER_TIME = re.compile(rf"\[(?P<elapsed>{CLOCK})<(?P<remaining>{CLOCK}|\?)\s*,")
RATE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<left>it|step|trial|episode|batch|s)/(?P<right>it|step|trial|episode|batch|s)\b", re.I)
TOTAL_TIME = re.compile(rf"Total time:\s*({CLOCK})", re.I)
IGNORE = re.compile(r"Traceback|^\s*File [\"']|warnings?\b|Error:|EarlyStopping|early[ _-]?stop|step_embed", re.I)


def number(text: str) -> float:
    factor = {"k": 1e3, "m": 1e6, "g": 1e9}.get(text[-1:].lower(), 1)
    return float(text[:-1] if factor != 1 else text) * factor


def seconds(text: str) -> float:
    days = re.match(r"(\d+)\s+days?,\s*", text)
    result = int(days[1]) * 86400 if days else 0
    pieces = text[days.end():].split(":") if days else text.split(":")
    value = 0
    for piece in pieces:
        value = value * 60 + int(piece)
    return result + value


class LogProgressParser:
    """状态可以序列化到 JSON，重启后保留总量、短周期样本和未闭合的终端文本。"""

    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}
        self.state.setdefault("totals", {})
        self.state.setdefault("outer", [])
        self.state.setdefault("cycles", [])
        self.state.setdefault("phase", "")

    def feed(self, data: bytes) -> None:
        # 保存 UTF-8 跨块字节，回车和换行都作为输出边界；内存上限避免长行撑大数据库。
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        decoder.setstate((bytes.fromhex(self.state.get("decoder", "")), 0))
        text = decoder.decode(data)
        self.state["decoder"] = decoder.getstate()[0].hex()
        text = self.state.get("pending", "") + text
        lines = re.split(r"[\r\n]", text)
        for line in lines[:-1]:
            self.line(line[:16384])
        self.state["pending"] = lines[-1][-16384:]

    def flush_meter(self) -> None:
        # tqdm 当前帧通常没有结尾换行，只有闭合时间段才提前识别，避免半个分母污染总量。
        line = self.state.get("pending", "")
        if METER_TIME.search(line) and "]" in line[METER_TIME.search(line).end():]:
            self.line(line)

    def _outer(self, unit: str, current: int, total: int | None) -> None:
        outer = self.state["outer"]
        index = next((i for i, item in enumerate(outer) if item["unit"] == unit), len(outer))
        old = outer[index] if index < len(outer) else None
        if old is not None and old["current"] != current:
            cycle = self.state.get("cycle", {})
            # 周期只在相邻轮次之间归档；重置、跳号和多层外循环切换不能混用速度。
            if current == old["current"] + 1 and index == len(outer) - 1:
                duration = cycle.get("outer_elapsed", 0) if cycle.get("nested") else cycle.get("train", 0) + cycle.get("eval", 0)
                if duration > 0 and cycle.get("train_done"):
                    self.state["cycles"] = (self.state["cycles"] + [duration])[-7:]
                    train = cycle.get("train_elapsed") if cycle.get("nested") else cycle.get("train")
                    if train is not None:
                        sample = {"total": duration, "train": train, "eval": max(0, duration - train)}
                        self.state["cycle_samples"] = (self.state.get("cycle_samples", []) + [sample])[-7:]
            else:
                self.state["cycles"] = []
                self.state.pop("cycle_samples", None)
            self.state.pop("inner", None)
            self.state["cycle"] = {}
            self.state["phase"] = ""
        base = old.get("base") if old else (current if current in (0, 1) else None)
        if base is None and current in (0, 1):
            # 配置行/恢复训练可能先出现大数字，后续实际出现 0/1 时允许纠正编号起点。
            base = current
        item = {"unit": unit, "current": current, "total": total or self.state["totals"].get(unit), "base": base}
        self.state["outer"] = (outer[:index] + [item])[-6:]

    def line(self, raw: str) -> None:
        line = ANSI.sub("", raw).strip()
        if not line:
            return
        signature = hashlib.sha256(line.encode()).hexdigest()
        if self.state.get("last_line") == signature:
            return
        self.state["last_line"] = signature
        rank = re.match(r"(?:\[rank(\d+)\]|rank\s*(\d+):)\s*", line, re.I)
        if rank:
            if int(rank[1] or rank[2]) != 0:
                return
            line = line[rank.end():]
        if "[NebulaGrid] task started at" in line:
            # 旧版本日志会追加多次执行；新的启动边界使旧配置和计时失效。
            self.state.clear()
            self.__init__(self.state)
            return
        if IGNORE.search(line):
            return
        seed = re.match(r"\[seed\s+(\d+)\]", line, re.I)
        if seed:
            # seed 编号不是总实验次数；批处理的总量未知时只承诺当前 seed 内的剩余时间。
            if self.state.get("seed") != seed[1]:
                for field in ("inner", "cycle", "live"):
                    self.state.pop(field, None)
                self.state["outer"] = []
                self.state["cycles"] = []
                self.state.pop("cycle_samples", None)
            self.state["seed"] = seed[1]
        phase_match = PHASE.search(line)
        named = list(NAMED.finditer(line))
        progress_record = bool(FRACTION.search(line) and (named or phase_match))
        if not progress_record and re.search(rf"\bper\s+(?:{WORDS})s?\s*[:=]", line, re.I):
            # 真实日志的 'Number of training training per epoch = 5004' 是每轮批数，
            # 不能误作 Epoch 5004；每轮 step 总量以随后实际进度条的分母为准。
            return
        # 配置段优先解析，不把 Training Epoch:1000、保存间隔、学习率 STEPS 当进度。
        declaration = False
        for match in TOTAL.finditer(line):
            unit = ALIASES[match["unit"].lower()]
            if progress_record:
                # MoE 的 steps/executed_steps 是模型指标；带实际计数的运行行优先解析进度。
                continue
            if (match["prefix"] or match["plural"]) and not re.search(r"[/\d]", line[match.end():match.end()+1]):
                count = int(match["n"])
                if 0 < count <= 10**12:
                    self.state["totals"][unit] = count
                    declaration = True
        for match in re.finditer(rf"(?:Start training for\s+|--)(?:(?P<unit>{WORDS})s?\s+)?(?P<n>\d+)(?:\s+(?P<suffix>{WORDS})s)?", line, re.I):
            alias = match["unit"] or match["suffix"]
            if alias:
                self.state["totals"][ALIASES[alias.lower()]] = int(match["n"])
                declaration = True
        if declaration:
            for item in self.state["outer"]:
                item["total"] = self.state["totals"].get(item["unit"], item.get("total"))
            return
        if re.search(r"Namespace\(|\[Config\]|(?:save|warmup|checkpoint|patience|val_period|steps:)\b", line, re.I):
            return
        # 只有明确的阶段前缀改变当前阶段；Train Loss / Test Loss 汇总不代表阶段切换。
        if phase_match:
            phase = PHASES[phase_match["name"].lower()]
            if phase != self.state["phase"]:
                self.state.pop("inner", None)
            self.state["phase"] = phase
        if self.state["phase"] == "训练前检查" and not phase_match and named:
            self.state.pop("inner", None)
            self.state["phase"] = ""
        for match in named:
            unit = ALIASES[match["unit"].lower()]
            if unit != "step":
                total = int(match["total"]) if match["total"] else None
                self._outer(unit, int(match["n"]), total)
        if named and not phase_match and any(m["unit"].lower() == "epoch" for m in named):
            # 验证结束后外层 Epoch 会再次刷新，必须回到外层计时，不能继续当作验证条。
            if self.state["phase"] != "训练":
                self.state.pop("inner", None)
            self.state["phase"] = "训练"
        cycle = self.state.setdefault("cycle", {})
        done = re.search(r"\bdone:.*\btime=(\d+(?:\.\d+)?)s\b", line, re.I)
        if done and named:
            cycle.update(train=float(done[1]), train_done=True)
            return
        duration = TOTAL_TIME.search(line)
        if duration:
            value = seconds(duration[1])
            if any(ALIASES[m["unit"].lower()] != "step" for m in named):
                cycle["train"] = value
                cycle["train_done"] = True
            elif self.state["phase"] in {"验证", "测试", "评估"}:
                # 同一阶段可能有普通模型和 EMA 两轮评估，每次完成都计入本轮。
                cycle["eval"] = cycle.get("eval", 0) + value
            return
        pairs = list(FRACTION.finditer(line))
        if not pairs:
            return
        bar = "%|" in line or METER_TIME.search(line) is not None
        # 没有内层计数时，Episode 3/100 等本身就是外层进度，不重复生成 Step。
        if not bar and len(pairs) == 1 and any(m["total"] and ALIASES[m["unit"].lower()] != "step" for m in named):
            return
        if not bar and not named and not phase_match:
            return
        pair = pairs[-1] if not bar else next((p for p in pairs if p.start() > line.find("|")), pairs[-1])
        current, total = number(pair[1]), number(pair[2])
        if not 0 <= current <= total <= 10**12 or total <= 0:
            return
        timing = METER_TIME.search(line)
        rate = RATE.search(line)
        unit = "step"
        if rate:
            alias = rate["right"] if rate["left"].lower() == "s" else rate["left"]
            unit = ALIASES.get(alias.lower(), "step")
        old = self.state.get("inner", {})
        if bar and old.get("bar") and old.get("total") != total and not phase_match and self.state["outer"]:
            # 同名长/短条缺少 train/val 标签时只估本阶段，不用不同分母重写整轮速度。
            self.state["ambiguous_phase"] = True
        identity = (unit, total, self.state["phase"])
        same = (old.get("unit"), old.get("total"), old.get("phase")) == identity and current >= old.get("current", 0)
        inner = {"unit": unit, "current": current, "total": total, "phase": self.state["phase"], "bar": bool(bar)}
        if same:
            for key in ("rates", "sample_seconds"):
                if key in old:
                    inner[key] = old[key]
        if timing:
            inner["elapsed"] = seconds(timing["elapsed"])
            if timing["remaining"] != "?":
                inner["remaining"] = seconds(timing["remaining"])
        if rate and float(rate["value"]) > 0:
            value = float(rate["value"])
            per_step = value if rate["left"].lower() == "s" else 1 / value
            # 同一步的 postfix 刷新不重复加入速度样本；只保留少量近期速度平滑抖动。
            if not same or current > old.get("current", -1):
                inner["rates"] = (inner.get("rates", []) + [per_step])[-9:]
        eta = re.search(rf"\beta:\s*({CLOCK})", line, re.I)
        if eta:
            inner["remaining"] = seconds(eta[1])
        self.state["inner"] = inner
        if bar and self.state["outer"] and named and self.state["phase"] not in {"验证", "测试", "评估", "训练前检查"}:
            # Lightning 外层 elapsed 在验证期间继续累加，完成周期时只能采用外层总耗时。
            cycle["nested"] = True
            # Lightning 可能先把同名旧 epoch 的进度条归零，再更新 epoch 标题；保留本轮最大计时。
            cycle["outer_elapsed"] = max(cycle.get("outer_elapsed", 0), inner.get("elapsed", 0))
            if current == total:
                if not cycle.get("train_done"):
                    cycle["train_elapsed"] = inner.get("elapsed", 0)
                cycle["train_done"] = True

    def observe(self, now: float) -> None:
        """只有追到文件末尾后的真实扫描快照才能测速，补读记录不伪造发生时间。"""
        self.flush_meter()
        inner = self.state.get("inner")
        outer = self.state["outer"]
        target = inner or (outer[-1] if outer else None)
        if not target:
            return
        key = repr([(x["unit"], x["current"]) for x in outer]) if inner else target["unit"]
        key += repr((target["unit"], target.get("total"), self.state["phase"]))
        old = self.state.get("live", {})
        changed = old.get("key") != key or old.get("current") != target["current"]
        if changed:
            self.state["last_progress_at"] = now
        if old.get("key") == key and target["current"] > old.get("current", 0) and now > old.get("at", now):
            value = (now - old["at"]) / (target["current"] - old["current"])
            target["sample_seconds"] = value
            if not inner:
                self.state["cycles"] = (self.state["cycles"] + [value])[-7:]
        if changed or not old:
            self.state["live"] = {"key": key, "current": target["current"], "at": now}

    def summary(self, now: float, interval: int = 60) -> dict:
        outer, inner = self.state["outer"], self.state.get("inner")
        parts = [f"{LABELS[x['unit']]} {x['current']}/{x['total']}" if x.get("total") else
                 f"{LABELS[x['unit']]} {x['current']}" for x in outer]
        if inner:
            parts.append(f"{LABELS[inner['unit']]} {inner['current']:g}/{inner['total']:g}")
        if self.state.get("seed"):
            parts.insert(0, f"Seed {self.state['seed']}")
        phase = self.state["phase"]
        result = {"text": " · ".join(([phase] if phase else []) + parts) or "尚未识别进度",
                  "remaining_seconds": None, "scope": "unknown", "reason": "总量未知或正在采样",
                  "updated_at": self.state.get("last_progress_at"), "stale": False}
        age = now - self.state.get("last_progress_at", now)
        if age > max(180, interval * 3):
            result.update(stale=True, reason="进度长时间未更新")
            return result
        remaining = None
        if inner and inner["current"] > 0 and phase != "训练前检查":
            rate = median(inner["rates"]) if inner.get("rates") else inner.get("sample_seconds")
            remaining = (inner["total"] - inner["current"]) * rate if rate else inner.get("remaining")
            result.update(scope="stage", reason="按当前阶段进度估算")
        cycles = self.state["cycles"]
        if (len(outer) == 1 and cycles and outer[0].get("total") and outer[0].get("base") is not None
                and not self.state.get("ambiguous_phase")):
            item = outer[0]
            rounds = item["total"] - (item["current"] - item["base"] + 1)
            cycle_seconds = median(cycles)
            cycle = self.state.get("cycle", {})
            consumed = cycle.get("outer_elapsed", 0) if cycle.get("nested") else cycle.get("train", 0) + cycle.get("eval", 0)
            if inner and not cycle.get("nested"):
                consumed += inner.get("elapsed", 0)
                if not inner.get("elapsed") and inner.get("remaining") is not None and phase == "训练":
                    # 完整轮次样本已含验证，剩余训练只用于估算当前轮的已用时间。
                    consumed = max(0, cycle_seconds * inner["current"] / inner["total"])
            current_remaining = max(0, cycle_seconds - consumed)
            samples = self.state.get("cycle_samples", [])
            if samples and inner:
                eval_seconds = median([sample["eval"] for sample in samples])
                if phase == "训练" and remaining is not None:
                    # 日志内 ETA 只覆盖训练；本轮还要加验证/测试，后续轮次用完整周期。
                    current_remaining = remaining + eval_seconds
                elif phase in {"验证", "测试", "评估"}:
                    eval_left = max(0, eval_seconds - cycle.get("eval", 0) - inner.get("elapsed", 0))
                    current_remaining = max(remaining or 0, eval_left)
            remaining = max(0, rounds) * cycle_seconds + current_remaining
            result.update(scope="stage" if self.state.get("seed") else "task",
                          reason=f"按最近 {len(cycles)} 个已识别周期估算，未出现的后续阶段不计入")
        elif (len(outer) == 1 and outer[0].get("total") and outer[0].get("base") is not None
              and inner and remaining is not None and phase == "训练"
              and not self.state.get("ambiguous_phase")):
            # 尚无完整周期也可按本轮训练速度外推，但必须注明尚未计入验证等后续阶段。
            # 总轮次本身不提供编号基准；没有观察到 0/1 的恢复任务继续保持阶段估计。
            item = outer[0]
            rate = median(inner["rates"]) if inner.get("rates") else inner.get("sample_seconds")
            cycle_seconds = inner["total"] * rate if rate else inner.get("elapsed", 0) + remaining
            if not rate and not inner.get("elapsed"):
                left = inner["total"] - inner["current"]
                cycle_seconds = remaining * inner["total"] / left if left > 0 else 0
            if cycle_seconds > 0:
                rounds = max(0, item["total"] - (item["current"] - item["base"] + 1))
                remaining += rounds * cycle_seconds
                result.update(scope="stage" if self.state.get("seed") else "task", estimate_kind="rough",
                              reason="按总轮次和本轮训练速度粗估，尚未计入验证/测试耗时")
        if remaining is not None and math.isfinite(remaining) and remaining >= 0:
            result["remaining_seconds"] = round(remaining)
        return result
