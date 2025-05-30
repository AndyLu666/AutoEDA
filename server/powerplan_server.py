#!/usr/bin/env python3
"""
MCP · Power-plan Service   (PG-stripe generation)

Start:
    cd ~/proj/mcp-eda-example
    python3 server/powerplan_server.py        # 监听 0.0.0.0:3336

Example:
    curl -X POST http://localhost:3336/power/run \
         -H "Content-Type: application/json" \
         -d '{"design":"des","tech":"FreePDK45","impl_ver":"cpV1_clkP1_drcV1__g0_p0","force":true,"top_module":"des3"}'
"""
from typing import Optional
import subprocess
import pathlib
import datetime
import os
import logging
import sys
import gzip
import glob
import csv
from fastapi import FastAPI
from pydantic import BaseModel

# ────────────────── 环境配置 & PATH ──────────────────
os.environ["PATH"] = (
    "/opt/cadence/innovus221/tools/bin:"
    "/opt/cadence/genus172/bin:" + os.environ.get("PATH", "")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/yl996/pwr_api.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

# ────────────────── 常量 ──────────────────
ROOT    = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "scripts" / "FreePDK45" / "backend"
LOG_DIR = ROOT / "logs" / "powerplan"; LOG_DIR.mkdir(parents=True, exist_ok=True)

IMP_CSV = ROOT / "config" / "imp_global.csv"

# ────────────────── 请求/响应模型 ──────────────────
class PwrReq(BaseModel):
    design:     str
    tech:       str = "FreePDK45"
    impl_ver:   str
    force:      bool = False
    top_module: Optional[str] = None

class PwrResp(BaseModel):
    status:    str
    log_path:  str
    report:    str

# ────────────────── 工具函数 ──────────────────
def read_csv_row(path: pathlib.Path, idx: int) -> dict:
    rows = list(csv.DictReader(path.open()))
    if idx >= len(rows):
        raise IndexError(f"{path.name}: row {idx} out of range (total {len(rows)})")
    return rows[idx]

def parse_top_from_config(cfg: pathlib.Path) -> str:
    if not cfg.exists():
        return ""
    for line in cfg.read_text().splitlines():
        if line.startswith("set TOP_NAME"):
            return line.split('"')[1]
    return ""

def run(cmd: str, log_file: pathlib.Path, cwd: pathlib.Path, env_extra: dict):
    env = os.environ.copy()
    env.update(env_extra)
    with log_file.open("w") as lf, subprocess.Popen(
        cmd,
        cwd=cwd,
        shell=True,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        executable="/bin/bash",
        env=env,
    ) as p:
        for line in p.stdout:
            lf.write(line)
    if p.returncode != 0:
        raise RuntimeError(f"command exited {p.returncode}")

# ────────────────── FastAPI App ──────────────────
app = FastAPI(title="MCP · Power-plan Service")

@app.post("/power/run", response_model=PwrResp)
def powerplan(req: PwrReq):
    des_root = ROOT / "designs" / req.design / req.tech
    impl_dir = des_root / "implementation" / req.impl_ver
    if not impl_dir.exists():
        return PwrResp(status="error: implementation dir not found", log_path="", report="")

    enc_dat = impl_dir / "pnr_save" / "floorplan.enc.dat"
    if not enc_dat.exists():
        return PwrResp(status="error: floorplan.enc.dat not found", log_path="", report="")

    rpt_dir  = impl_dir / "pnr_reports"
    rpt_file = rpt_dir / "powerplan.rpt"
    if req.force and rpt_file.exists():
        rpt_file.unlink()

    # --- determine top module ---
    cfg_path = ROOT / "designs" / req.design / "config.tcl"
    if req.top_module:
        top = req.top_module
    else:
        parsed = parse_top_from_config(cfg_path)
        top = parsed if parsed else req.design

    # --- prepare env ---
    env = {"BASE_DIR": str(ROOT)}
    env.update(read_csv_row(IMP_CSV, 0))  # 如果 g_idx 可配置，可改为 req.g_idx
    env.setdefault("TOP_NAME", top)
    env.setdefault("FILE_FORMAT", "verilog")

    # --- Innovus 命令构造 ---
    config_tcl = ROOT / "config.tcl"
    tech_tcl   = ROOT / "scripts" / req.tech / "tech.tcl"
    power_tcl  = BACKEND / "3_powerplan.tcl"

    scripts = [str(config_tcl), str(tech_tcl), str(power_tcl)]
    files_arg = " ".join(scripts)

    exec_cmd = (
        f'source "{config_tcl}"; '
        f'source "{tech_tcl}"; '
        f'restoreDesign "{enc_dat}" {top}; '
        f'source "{power_tcl}"; '
        'report_power > pnr_reports/powerplan.rpt'
    )
    innovus_cmd = (
        f'innovus -no_gui -batch '
        f'-execute "{exec_cmd}" '
        f'-files "{files_arg}"'
    )

    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"{req.design}_pwr_{ts}.log"

    try:
        run(innovus_cmd, log_file, impl_dir, env)
    except Exception as e:
        return PwrResp(status=f"error: {e}", log_path=str(log_file), report="")

    # --- collect report ---
    report_text = "powerplan.rpt(.gz) not found"
    if rpt_file.exists():
        report_text = rpt_file.read_text(errors="ignore")
    else:
        for cand in glob.glob(str(rpt_dir / "floorplan_summary.rpt*")):
            p = pathlib.Path(cand)
            if p.suffix == ".gz":
                with gzip.open(p, "rt") as f:
                    report_text = f.read()
            else:
                report_text = p.read_text(errors="ignore")
            break

    return PwrResp(status="ok", log_path=str(log_file), report=report_text)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("powerplan_server:app", host="0.0.0.0", port=3336, reload=False)