import queue
import threading
import tkinter as tk
from concurrent.futures import Future
from tkinter import messagebox, scrolledtext, simpledialog, ttk

import core

CHANNELS_HINT = (
    "# По одному каналу на строку: @имя или ссылка t.me/имя\n"
    "# После знака | можно написать свой текст для этого канала.\n"
    "# В тексте: {title} — название ролика, {url} — ссылка, \\n — новая строка.\n"
    "# Без | используется «Текст по умолчанию».\n"
    "#\n"
    "# @example_news | Свежий выпуск: {title}\\n{url}\n"
    "# @example_chat | Смотрите, что вышло 👉 {url}\n"
    "# @example_default\n"
)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Рассылка роликов YouTube в Telegram")
        root.geometry("760x820")
        self.cfg = core.load_config()
        self.engine: core.Engine | None = None
        self.ui_queue: queue.Queue = queue.Queue()
        self.vars: dict[str, tk.StringVar] = {}

        pad = {"padx": 10, "pady": 4}

        # --- Настройки ---
        box = ttk.LabelFrame(root, text="1. Настройки")
        box.pack(fill="x", **pad)
        box.columnconfigure(1, weight=1)
        rows = [
            ("api_id", "Telegram API ID:", False),
            ("api_hash", "Telegram API Hash:", False),
            ("youtube", "Ваш YouTube-канал (ссылка или ID):", False),
        ]
        for i, (key, label, _) in enumerate(rows):
            ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=3)
            self.vars[key] = tk.StringVar(value=str(self.cfg[key]))
            ttk.Entry(box, textvariable=self.vars[key]).grid(row=i, column=1, sticky="ew", padx=8)
        ttk.Label(
            box, foreground="gray",
            text="API ID и Hash берутся на my.telegram.org → API development tools",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=8)

        ttk.Label(box, text="Текст по умолчанию:").grid(row=4, column=0, sticky="nw", padx=8, pady=3)
        self.template = tk.Text(box, height=3, wrap="word")
        self.template.insert("1.0", self.cfg["template"])
        self.template.grid(row=4, column=1, sticky="ew", padx=8)

        timing = ttk.Frame(box)
        timing.grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=6)
        for key, label, w in [
            ("check_minutes", "Проверять YouTube каждые (мин):", 4),
            ("delay_min", "  Пауза между каналами, с — от:", 4),
            ("delay_max", "до:", 4),
        ]:
            ttk.Label(timing, text=label).pack(side="left")
            self.vars[key] = tk.StringVar(value=str(self.cfg[key]))
            ttk.Spinbox(timing, from_=1, to=3600, width=w, textvariable=self.vars[key]).pack(side="left")

        # --- Каналы ---
        box2 = ttk.LabelFrame(root, text="2. Каналы, куда писать комментарии")
        box2.pack(fill="both", expand=True, **pad)
        self.channels = scrolledtext.ScrolledText(box2, height=9, wrap="none")
        self.channels.insert("1.0", self.cfg["channels"] or CHANNELS_HINT)
        self.channels.pack(fill="both", expand=True, padx=8, pady=6)

        # --- Управление ---
        btns = ttk.Frame(root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="▶ Запустить", command=lambda: self.start(False))
        self.start_btn.pack(side="left")
        self.test_btn = ttk.Button(
            btns, text="Тест: разослать последний ролик сейчас", command=lambda: self.start(True)
        )
        self.test_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(btns, text="■ Стоп", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.status = ttk.Label(btns, text="Не запущено", foreground="gray")
        self.status.pack(side="right")

        # --- Журнал ---
        box3 = ttk.LabelFrame(root, text="Журнал")
        box3.pack(fill="both", expand=True, **pad)
        self.logbox = scrolledtext.ScrolledText(box3, height=10, state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=8, pady=6)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.pump()

    # --- потокобезопасная связь с движком ---
    def pump(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(150, self.pump)

    def log(self, text: str):
        def write():
            from datetime import datetime
            self.logbox.configure(state="normal")
            self.logbox.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.ui_queue.put(write)

    def ask(self, kind: str, prompt: str):
        fut: Future = Future()

        def dialog():
            fut.set_result(simpledialog.askstring(
                "Вход в Telegram", prompt, parent=self.root, show="*" if kind == "password" else None
            ))
        self.ui_queue.put(dialog)
        return fut.result()

    # --- действия ---
    def collect(self) -> dict | None:
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        cfg["template"] = self.template.get("1.0", "end").strip() or core.DEFAULT_TEMPLATE
        cfg["channels"] = self.channels.get("1.0", "end").strip()
        try:
            for k in ("check_minutes", "delay_min", "delay_max"):
                cfg[k] = int(cfg[k])
            int(cfg["api_id"])
        except ValueError:
            messagebox.showerror("Ошибка", "API ID и числовые настройки должны быть числами.")
            return None
        if not (cfg["api_id"] and cfg["api_hash"] and cfg["youtube"]):
            messagebox.showerror("Ошибка", "Заполните API ID, API Hash и YouTube-канал.")
            return None
        if not core.parse_channels(cfg["channels"], cfg["template"]):
            messagebox.showerror("Ошибка", "Добавьте хотя бы один канал.")
            return None
        return cfg

    def set_running(self, running: bool):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.test_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.status.configure(text="Работает" if running else "Не запущено",
                              foreground="green" if running else "gray")

    def start(self, send_latest: bool):
        cfg = self.collect()
        if not cfg:
            return
        if send_latest and not messagebox.askyesno(
            "Тест", "Последний ролик будет сейчас отправлен во ВСЕ указанные каналы. Продолжить?"
        ):
            return
        core.save_config(cfg)
        self.engine = core.Engine(cfg, self.log, self.ask)
        self.set_running(True)

        def work():
            self.engine.run(send_latest)
            self.ui_queue.put(lambda: self.set_running(False))
        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        if self.engine:
            self.log("Останавливаю...")
            self.engine.stop()

    def on_close(self):
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        cfg["template"] = self.template.get("1.0", "end").strip() or core.DEFAULT_TEMPLATE
        cfg["channels"] = self.channels.get("1.0", "end").strip()
        core.save_config(cfg)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
