"""Large-text practice display, run as the desktop user over private stdio pipes."""
import asyncio
import json
import os
import sys

from .video import desktop_process_options


def presentation(state):
    """Pure view model; no hardware decisions or timing commands live in the UI."""
    side = "왼쪽" if state.get("side") == "left" else "오른쪽"
    manual = state["condition"] == "baseline"
    active = state["active"]
    if not active:
        title, instruction = "연습할 손을 선택하세요", "전극 천을 떼고, 위의 왼쪽 또는 오른쪽 버튼을 누르세요."
    elif not state["allowed"]:
        title, instruction = f"{side} 사전 안내", "장갑에서 첫 진동이 느껴지면 입력할 수 있어요."
    elif state["cancelled"]:
        title, instruction = "자동 점등을 취소했어요", "이번 회차는 켜지지 않아요. 전극을 떼고 다음 회차를 기다리세요."
    elif state["lit"]:
        title = f"{side} 방향지시등 켜짐"
        instruction = ("다시 맞대도 켜진 상태를 유지해요. 통과 시 자동으로 꺼져요." if manual else
                       "반자동은 켜진 뒤에는 취소할 수 없어요. 통과 시 자동으로 꺼져요.")
    elif manual:
        title, instruction = f"{side} 전극을 맞대 보세요", "한 번 맞대면 켜져요. 이후 다시 맞대도 유지되고, 통과 시 자동으로 꺼져요."
    else:
        title, instruction = f"{side} 자동 점등 대기", "취소하려면 지금 전극을 맞대세요. 가만히 있으면 자동으로 켜져요."
    elapsed = state.get("elapsed_s", 0)
    remaining = max(0, state["pass_s"] - elapsed) if active else 0
    time_text = f"회차 종료까지 {remaining:.1f}초" if active else "시간 제한 없이 원하는 만큼 반복할 수 있어요."
    if active and not manual and not state["lit"] and not state["cancelled"]:
        time_text += f"   ·   자동 점등까지 {max(0, state['auto_on_s'] - elapsed):.1f}초"
    return dict(title=title, instruction=instruction, time_text=time_text,
                progress=min(100, max(0, elapsed / state["pass_s"] * 100)) if active else 0)


class PracticeWindow:
    def __init__(self, queue):
        self.queue = queue
        self.process = None
        self.reader_task = self.stderr_task = None
        self.ready = None
        self.closing = False
        self.stderr = ""

    async def open(self):
        options = desktop_process_options() if sys.platform.startswith("linux") else {"env": os.environ.copy()}
        options["env"]["PYTHONUTF8"] = "1"
        self.ready = asyncio.get_running_loop().create_future()
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "vibro.practice_window", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)
        self.reader_task = asyncio.create_task(self.read())
        self.stderr_task = asyncio.create_task(self.read_errors())
        try:
            await asyncio.wait_for(asyncio.shield(self.ready), 10)
        except Exception as error:
            await self.close()
            raise RuntimeError("연습 창을 열지 못했습니다. Pi 데스크톱에서 실행하고, "
                               "필요하면 sudo apt install python3-tk 를 설치하세요. "
                               "터미널 연습: bash run practice --terminal. " + self.stderr[-500:]) from error

    async def read_errors(self):
        while data := await self.process.stderr.read(1024):
            self.stderr = (self.stderr + data.decode("utf-8", "replace"))[-2000:]

    async def read(self):
        import time
        try:
            while line := await self.process.stdout.readline():
                command = json.loads(line).get("command")
                if command == "ready":
                    if not self.ready.done():
                        self.ready.set_result(True)
                elif command in ("1", "2", "b", "p", "r", "0", "q"):
                    self.queue.put_nowait(("operator", command, time.monotonic()))
        except Exception as error:
            self.stderr += str(error)
        finally:
            if not self.ready.done():
                self.ready.set_exception(RuntimeError("practice window closed before ready"))
            if not self.closing:
                self.queue.put_nowait(("operator", "eof", time.monotonic()))

    async def publish(self, state):
        self.process.stdin.write((json.dumps(state, ensure_ascii=False) + "\n").encode("utf-8"))
        await asyncio.wait_for(self.process.stdin.drain(), 1)

    async def close(self):
        self.closing = True
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                self.process.terminate()
                await self.process.wait()
        for task in (self.reader_task, self.stderr_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.reader_task, self.stderr_task) if t), return_exceptions=True)


def main():
    import queue
    import threading
    import tkinter as tk
    from tkinter import ttk

    messages = queue.Queue()
    root = tk.Tk()
    root.title("스마트 장갑 · 본 실험 전 연습")
    root.geometry(f"{min(1120, root.winfo_screenwidth()-60)}x{min(780, root.winfo_screenheight()-80)}+20+20")
    root.configure(bg="#eef2f7")
    font = "맑은 고딕" if sys.platform == "win32" else "Noto Sans CJK KR"
    root.option_add("*Font", (font, 14))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(4, weight=1)
    closing = False

    def send(command):
        nonlocal closing
        if closing:
            return
        print(json.dumps({"command": command}), flush=True)
        if command == "q":
            closing = True
            title.set("연습을 종료하고 있어요")
            root.after(2000, root.destroy)

    def label(parent, text="", **options):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg="#14233b", **options)

    header = tk.Frame(root, bg="#eef2f7")
    header.grid(row=0, column=0, sticky="ew", padx=26, pady=(18, 8))
    label(header, "장갑 조작 연습", font=(font, 26, "bold")).pack(anchor="w")
    label(header, "전극을 맞대고 → 떼고 → 다시 맞대며 익혀 보세요.").pack(anchor="w")

    buttons = {}
    def button_row(row, choices):
        frame = tk.Frame(root, bg="#eef2f7")
        frame.grid(row=row, column=0, sticky="ew", padx=26, pady=5)
        for column, (key, text) in enumerate(choices):
            frame.columnconfigure(column, weight=1)
            button = tk.Button(frame, text=text, command=lambda c=key: send(c),
                               bg="white", fg="#14233b", activebackground="#dbeafe",
                               relief="flat", bd=0, padx=8, pady=12, font=(font, 16, "bold"))
            button.grid(row=0, column=column, sticky="ew", padx=4)
            buttons[key] = button
    button_row(1, [("b", "수동 · 직접 켜기"), ("p", "반자동 · 자동 점등 / 취소")])
    button_row(2, [("1", "←  왼쪽 연습 시작"), ("2", "오른쪽 연습 시작  →")])

    contacts = tk.Frame(root, bg="#eef2f7")
    contacts.grid(row=3, column=0, sticky="ew", padx=30, pady=6)
    contacts.columnconfigure((0, 1), weight=1)
    contact_labels = {}
    for i, side in enumerate(("left", "right")):
        widget = label(contacts, "왼쪽 전극: 떼어짐" if i == 0 else "오른쪽 전극: 떼어짐")
        widget.grid(row=0, column=i, sticky="ew")
        contact_labels[side] = widget

    card = tk.Frame(root, bg="white")
    card.grid(row=4, column=0, sticky="nsew", padx=30, pady=8)
    title, instruction, timing, result = (tk.StringVar() for _ in range(4))
    title.set("연결 상태를 확인하고 있어요")
    title_label = label(card, textvariable=title, font=(font, 28, "bold"), wraplength=1000)
    title_label.pack(padx=20, pady=(22, 12))
    instruction_label = label(card, textvariable=instruction, font=(font, 18), wraplength=980)
    instruction_label.pack(padx=20, pady=(0, 12))
    label(card, textvariable=timing).pack(pady=4)
    progress = ttk.Progressbar(card, maximum=100)
    progress.pack(fill="x", padx=28, pady=(6, 18))
    result_label = label(card, textvariable=result, font=(font, 16, "bold"), wraplength=980)
    result_label.pack(padx=20, pady=(2, 18))
    button_row(5, [("r", "같은 손 다시"), ("0", "모두 끄기"), ("q", "연습 종료")])
    label(root, "본 실험 결과에 포함되지 않는 연습입니다.  ·  F11: 전체 화면", font=(font, 11)).grid(row=6, column=0, pady=(4, 14))

    def resize(event):
        if event.widget is root:
            title_label.config(wraplength=max(300, event.width - 100))
            instruction_label.config(wraplength=max(300, event.width - 100))
            result_label.config(wraplength=max(300, event.width - 100))
    root.bind("<Configure>", resize)
    for key in ("1", "2", "b", "p", "r", "0", "q"):
        root.bind(key, lambda event, c=key: send(c))
    root.bind("<F11>", lambda event: root.attributes("-fullscreen", not root.attributes("-fullscreen")))
    root.bind("<Escape>", lambda event: root.attributes("-fullscreen", False))
    root.protocol("WM_DELETE_WINDOW", lambda: send("q"))

    def receive():
        try:
            for line in sys.stdin:
                messages.put(json.loads(line))
        finally:
            messages.put(None)
    threading.Thread(target=receive, daemon=True).start()

    def refresh():
        latest = False
        while not messages.empty():
            latest = messages.get_nowait()
            if latest is None:
                root.destroy()
                return
        if latest and not closing:
            view = presentation(latest)
            title.set(view["title"])
            instruction.set(view["instruction"])
            timing.set(view["time_text"])
            result.set(latest.get("message", ""))
            progress["value"] = view["progress"]
            for key, condition in (("b", "baseline"), ("p", "proposed")):
                selected = latest["condition"] == condition
                buttons[key].config(bg="#1d4ed8" if selected else "white", fg="white" if selected else "#14233b")
            for side, widget in contact_labels.items():
                pressed = latest["contacts"][side]
                name = "왼쪽" if side == "left" else "오른쪽"
                widget.config(text=f"{name} 전극: {'맞닿음' if pressed else '떼어짐'}",
                              fg="#15803d" if pressed else "#475569")
        root.after(50, refresh)
    print(json.dumps({"command": "ready"}), flush=True)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
