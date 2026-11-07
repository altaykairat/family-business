"""Manual paste helper (sprint 1, item 0). LLM steps stay manual: a human pastes every prompt into a fresh chat.

  python3 scripts/paste.py next   --run R [--agent A]   # copy the next waiting prompt to the clipboard
  python3 scripts/paste.py save   --run R [--agent A]   # clipboard (model answer) → response, checked at once
  python3 scripts/paste.py status --run R               # how many are left
  python3 scripts/paste.py go     --run R [--agent A]   # loop: copy prompt → Enter after copying the answer → save → next

R = run id under runs/, a variant run id (variants/*/runs/R) or a path to a run folder.
A rejected answer is not saved — re-paste it (or `save --force` to keep it anyway).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cqa import manual  # noqa: E402
from cqa.common import CQAError  # noqa: E402


def do_next(a) -> bool:
    cur = manual.current(a.run, a.agent)
    if cur["name"] is None:
        print(f"✓ {cur['agent']}: все {cur['total']} промптов с ответами")
        return False
    manual.copy(cur["text"])
    print(f"→ в буфере: {cur['name']}  [{cur['agent']}, {cur['number']}/{cur['total']}, осталось {cur['left']}]"
          f"  ~{len(cur['text']) // 1000}k символов")
    return True


def do_save(a) -> bool:
    res = manual.save(a.run, manual.paste(), a.agent, a.name, a.force)
    for w in res["warnings"][:8]:
        print(f"  ! {w}")
    if res["saved"]:
        tail = " (с ошибками, --force)" if res["errors"] else ""
        print(f"✓ сохранено{tail}: {res['saved']}")
        return True
    print(f"✗ {res['name']}: ответ не принят — перевставь")
    for e in res["errors"][:8]:
        print(f"  ✗ {e}")
    return False


def do_status(a) -> None:
    st = manual.status(a.run, a.agent)
    print(st["run"])
    for ag, s in st["agents"].items():
        nxt = f", следующий {s['next']}" if s["next"] else ""
        print(f"  {ag}: {s['done']}/{s['total']} готово, осталось {s['waiting']}{nxt}")


def do_go(a) -> None:
    while do_next(a):
        while True:
            try:
                cmd = input("  вставь в новый чат, скопируй ответ целиком, Enter = сохранить (s = пропустить, q = выход): ")
            except EOFError:
                return
            if cmd.strip().lower() == "q":
                return
            if cmd.strip().lower() == "s":
                print("  пропуск — промпт останется первым в очереди")
                return
            if do_save(a):
                break


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Помощник ручной вставки промптов")
    ap.add_argument("cmd", choices=["next", "save", "status", "go"])
    ap.add_argument("--run", required=True)
    ap.add_argument("--agent")
    ap.add_argument("--name", help="save: конкретный промпт вместо текущего")
    ap.add_argument("--force", action="store_true", help="save: сохранить, даже если парсер отверг")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "next":
            do_next(a)
        elif a.cmd == "save":
            return 0 if do_save(a) else 1
        elif a.cmd == "status":
            do_status(a)
        else:
            do_go(a)
    except CQAError as e:
        print(f"ошибка: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
