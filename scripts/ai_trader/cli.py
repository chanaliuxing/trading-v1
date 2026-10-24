#!/usr/bin/env python3
import sys
from pathlib import Path

# Ensure project root is on sys.path so that "scripts.*" imports work
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import click
from scripts.ai_trader.orchestrator import Orchestrator


@click.group()
def cli():
    """AI Trader Control Plane CLI"""


@cli.command()
def resume():
    """Resume appropriate session based on NY weekday/time.

    Weekends: run learning only; Weekdays: route by session (morning/intraday/afterhours/overnight).
    """
    import datetime, pytz
    tz = pytz.timezone('America/New_York')
    now = datetime.datetime.now(tz)
    wd = now.weekday()
    o = Orchestrator()
    from libs.market_calendar import is_holiday
    if wd >= 5 or is_holiday(now.date()):
        res = o.evening_learning()
        o.write_plan("learn_weekend", res)
        click.echo({"session": "weekend_learn", **res})
        return
    hm = now.strftime("%H:%M")
    if "07:00" <= hm < "09:30":
        res = o.morning_sequence()
        o.write_plan("morning", res)
        click.echo({"session": "morning", **res})
    elif "09:30" <= hm < "16:00":
        res = o.intraday_sequence(mode="slow")
        o.write_plan("intraday", res)
        click.echo({"session": "intraday", **res})
    elif "16:00" <= hm < "20:00":
        res = o.afterhours_sequence()
        o.write_plan("afterhours", res)
        click.echo({"session": "afterhours", **res})
    else:
        res = o.overnight_sequence()
        o.write_plan("overnight", res)
        click.echo({"session": "overnight", **res})

def morning():
    o = Orchestrator()
    res = o.morning_sequence()
    o.write_plan("morning", res)
    click.echo(res)


@cli.command()
@click.option("--mode", type=click.Choice(["fast", "full"]), default="fast")
def intraday(mode):
    o = Orchestrator()
    res = o.intraday_sequence(mode)
    o.write_plan("intraday", res)
    click.echo(res)


@cli.command()
def afterhours():
    o = Orchestrator()
    res = o.afterhours_sequence()
    o.write_plan("afterhours", res)
    click.echo(res)


@cli.command()
def overnight():
    o = Orchestrator()
    res = o.overnight_sequence()
    o.write_plan("overnight", res)
    click.echo(res)


@cli.command()
def learn():
    """Run evening learning. If policies.use_workflow=true and Prefect is available,
    run the Prefect flow; otherwise use the built-in Orchestrator path."""
    # Try to read policies.use_workflow from cfg
    o = Orchestrator()
    use_wf = bool((o.cfg.get("policies") or {}).get("use_workflow", False))
    if use_wf:
        try:
            from scripts.ai_trader.flows.prefect_evening import evening_learning_flow
            res = evening_learning_flow()
        except Exception as e:
            # Fallback to orchestrator when Prefect not installed or flow error
            res = {"status": "error", "error": f"workflow_failed: {e}"}
            click.echo(res)
            res = o.evening_learning()
    else:
        res = o.evening_learning()
    o.write_plan("learn", res)
    click.echo(res)


if __name__ == "__main__":
    cli()
