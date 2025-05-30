#!/usr/bin/env python3
"""
MCP · Global & Detail Routing Service            (端口 3339)

Usage
-----
cd ~/proj/mcp-eda-example
python3 server/route_server.py        # 监听 0.0.0.0:3339

HTTP 例：
curl -X POST http://localhost:3339/route/run \
     -H "Content-Type: application/json" \
     -d '{"design":"des","tech":"FreePDK45","impl_ver":"cpV1_clkP1_drcV1__g0_p0","g_idx":0,"p_idx":0,"c_idx":0,"force":true}'
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
from typing import Dict

from fastapi import FastAPI
from pydantic import BaseModel, Field

# ────────────────────────── 0. 环境 & 日志 ──────────────────────────
BIN_DIRS = [
    "/opt/cadence/innovus221/tools/bin",
    "/opt/cadence/genus172/bin",
]
os.environ["PATH"] = ":".join(BIN_DIRS + [os.environ.get("PATH", "")])

ROOT = pathlib.Path(__file__).resolve().parent.parent        # ~/proj/mcp-eda-example
LOG_DIR = ROOT / "logs" / "route"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "route_api.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname).1s] %(name)s:%(lineno)d  %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5 << 20, backupCount=3),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("route_server")

# ────────────────────────── 1. 常量 ──────────────────────────
BACKEND_DIR_TMPL = ROOT / "scripts" / "{tech}" / "backend"      # 运行时 format
IMP_CSV = ROOT / "config" / "imp_global.csv"
PLC_CSV = ROOT / "config" / "placement.csv"
CTS_CSV = ROOT / "config" / "cts.csv"

ROUTE_RPTS = [
    "route_summary.rpt",
    "postRoute_drc_max1M.rpt",
    "congestion.rpt",
    "postOpt_drc_max1M.rpt",
    "route_timing.rpt.gz",
]

# ────────────────────────── 2. 数据模型 ──────────────────────────
class RtReq(BaseModel):
    design: str                                  # eg. "des"
    tech: str = "FreePDK45"                      # 工艺，以 scripts/{tech}/backend 为准
    impl_ver: str                                # 实现版本目录名
    g_idx: int = 0                               # imp_global.csv 行号
    p_idx: int = 0                               # placement.csv 行号
    c_idx: int = 0                               # cts.csv 行号
    force: bool = False                          # true=删除旧报告并覆盖 snapshot
    top_module: str | None = Field(
        None, description="override TOP_NAME; 留空则从 designs/{design}/config.tcl 里解析"
    )

class RtResp(BaseModel):
    status: str
    log_path: str
    rpt_paths: Dict[str, str]                    # {report_name: relative_path | 'not found'}

# ────────────────────────── 3. 工具函数 ──────────────────────────
def read_csv_row(path: pathlib.Path, idx: int) -> dict:
    rows = list(csv.DictReader(path.open()))
    if not rows:
        raise ValueError(f"{path.name} is empty")
    if idx >= len(rows):
        raise IndexError(f"{path.name}: row {idx} out of range (total {len(rows)})")
    return rows[idx]

TOP_RE = re.compile(r"""^\s*set\s+TOP_NAME\s+"([^"]+)""")
def parse_top_from_config(cfg: pathlib.Path) -> str | None:
    if not cfg.exists():
        return None
    for line in cfg.read_text().splitlines():
        m = TOP_RE.match(line)
        if m:
            return m.group(1)
    return None

def run(cmd: str, logfile: pathlib.Path, cwd: pathlib.Path, env_extra: dict):
    env = os.environ.copy()
    env.update(env_extra)

    logger.info("launching Innovus cmd → %s", cmd)
    logger.debug("merged env =\n%s", json.dumps(env, indent=2))

    with logfile.open("w") as lf, subprocess.Popen(
        cmd,
        cwd=cwd,
        shell=True,
        text=True,
        bufsize=1,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        executable="/bin/bash",
        env=env,
    ) as p:
        for line in iter(p.stdout.readline, ""):
            lf.write(line)
        p.wait()

    if p.returncode != 0:
        raise RuntimeError(f"Innovus exited with code {p.returncode}")

# ────────────────────────── 4. FastAPI ──────────────────────────
app = FastAPI(title="MCP · Routing Service")

@app.post("/route/run", response_model=RtResp)
def route_run(req: RtReq):
    """Run Innovus 7_route.tcl (global+detail routing)."""

    impl_dir = ROOT / "designs" / req.design / req.tech / "implementation" / req.impl_ver
    enc_dat  = impl_dir / "pnr_save" / "floorplan.enc.dat"
    if not impl_dir.exists():
        return RtResp(status="error: implementation dir not found", log_path="", rpt_paths={})
    if not enc_dat.exists():
        return RtResp(status="error: floorplan.enc.dat not found", log_path="", rpt_paths={})

    # reports 目录
    rpt_dir = impl_dir / "pnr_reports"
    rpt_dir.mkdir(exist_ok=True)
    if req.force:
        for rpt in ROUTE_RPTS:
            (rpt_dir / rpt).unlink(missing_ok=True)
        # 若需彻底重跑，可同时删除旧 route snapshot
        (impl_dir / "pnr_save" / "route.enc.dat").unlink(missing_ok=True)

    # 解析顶层名
    if req.top_module:
        top_name = req.top_module
    else:
        parsed = parse_top_from_config(ROOT / "designs" / req.design / "config.tcl")
        top_name = parsed or req.design

    # 组合环境变量（后覆盖前）
    env = {
        "BASE_DIR": str(ROOT),
        "TOP_NAME": top_name,
        "FILE_FORMAT": "verilog",
    }
    env.update(read_csv_row(IMP_CSV, req.g_idx))
    env.update(read_csv_row(PLC_CSV, req.p_idx))
    env.update(read_csv_row(CTS_CSV, req.c_idx))

    # backend 脚本路径
    backend_dir = pathlib.Path(str(BACKEND_DIR_TMPL).format(tech=req.tech))
    config_tcl  = ROOT / "config.tcl"
    tech_tcl    = ROOT / "scripts" / req.tech / "tech.tcl"
    route_tcl   = backend_dir / "7_route.tcl"

    exec_cmd = (
        f'source "{config_tcl}"; '
        f'source "{tech_tcl}"; '
        f'restoreDesign "{enc_dat}" "{top_name}"; '
        f'source "{route_tcl}"'
    )
    innovus_cmd = (
        "innovus -no_gui -batch "
        f'-execute "{exec_cmd}" '
        f'-files "{config_tcl} {tech_tcl} {route_tcl}"'
    )

    # 运行
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"{req.design}_route_{ts}.log"
    try:
        run(innovus_cmd, log_file, impl_dir, env)
    except Exception as e:
        logger.exception("routing failed")
        return RtResp(status=f"error: {e}", log_path=str(log_file), rpt_paths={})

    # 收集报告（只返回相对路径，避免巨大 JSON）
    rpt_paths: Dict[str, str] = {}
    for rpt in ROUTE_RPTS:
        fp = rpt_dir / rpt
        rpt_paths[rpt] = str(fp.relative_to(ROOT)) if fp.exists() else "not found"

    return RtResp(status="ok", log_path=str(log_file), rpt_paths=rpt_paths)

# ────────────────────────── 5. CLI ──────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("route_server:app", host="0.0.0.0", port=3339, reload=False)