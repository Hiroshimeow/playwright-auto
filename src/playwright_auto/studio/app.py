from __future__ import annotations

import argparse
import json
import tkinter as tk
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable

from .controller import StudioController
from .models import RunStatus, StudioEvent, StudioSettings, StudioState, WorkerModel, WorkerStatus
from .theme import COLORS, STATUS_COLORS, configure_theme
from .widgets import LogNotebook, PromptDialog, ResponseDialog, ScrollableWorkerList


@dataclass
class PendingFuture:
    future: Future[Any]
    label: str
    on_success: Callable[[Any], None] | None = None


class StudioApp(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        *,
        controller: StudioController | None = None,
        auto_connect: bool = True,
    ) -> None:
        configure_theme(master)
        super().__init__(master, style="Studio.TFrame")
        self.controller = controller or StudioController()
        self.auto_connect = auto_connect
        self._closed = False
        self._poll_after: str | None = None
        self._pending: list[PendingFuture] = []
        self._last_state_json = ""
        self._settings_loaded = False
        self._selected_page_id: str | None = None
        self.connection_text = tk.StringVar(value="OFFLINE")
        self.run_status_text = tk.StringVar(value="IDLE")
        self.summary_text = tk.StringVar(value="No tabs discovered")
        self.selected_text = tk.StringVar(value="Select a worker")
        self.task_var = tk.StringVar()
        self.rounds_var = tk.IntVar(value=1)
        self.timeout_var = tk.IntVar(value=600)
        self.context_var = tk.IntVar(value=12_000)
        self._build()
        self._render_state(self.controller.snapshot(), force=True)
        self._poll_after = self.after(100, self._poll)
        if auto_connect:
            self.after(200, self._connect)

    def _build(self) -> None:
        self.configure(padding=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._build_topbar()
        self._build_body()

    def _build_topbar(self) -> None:
        top = tk.Frame(self, bg=COLORS["bg"], padx=14, pady=11)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(3, weight=1)

        brand = tk.Frame(top, bg=COLORS["bg"])
        brand.grid(row=0, column=0, sticky="w", padx=(0, 18))
        tk.Label(
            brand,
            text="AI Multi-Agent Studio",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 15),
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="Persistent ChatGPT tabs · CDP 9222",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        connection = tk.Frame(top, bg=COLORS["panel_alt"], padx=9, pady=6)
        connection.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.connection_dot = tk.Label(
            connection,
            text="●",
            bg=COLORS["panel_alt"],
            fg=COLORS["red"],
            font=("Segoe UI", 10),
        )
        self.connection_dot.pack(side="left")
        tk.Label(
            connection,
            textvariable=self.connection_text,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", padx=(6, 0))

        ttk.Button(top, text="Connect", style="Quiet.TButton", command=self._connect).grid(
            row=0, column=2, sticky="w"
        )

        goal_box = tk.Frame(top, bg=COLORS["bg"])
        goal_box.grid(row=0, column=3, sticky="ew", padx=14)
        tk.Label(
            goal_box,
            text="GLOBAL GOAL",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w")
        self.goal_text = tk.Text(
            goal_box,
            height=2,
            wrap="word",
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["selection"],
            relief="flat",
            padx=9,
            pady=6,
            font=("Segoe UI", 10),
        )
        self.goal_text.pack(fill="x", expand=True)

        controls = tk.Frame(top, bg=COLORS["bg"])
        controls.grid(row=0, column=4, sticky="e")
        self.start_button = ttk.Button(
            controls,
            text="Start Collaborative Task",
            style="Accent.TButton",
            command=self._start_run,
        )
        self.start_button.pack(side="left", padx=(0, 7))
        self.pause_button = ttk.Button(
            controls,
            text="Pause",
            style="Quiet.TButton",
            command=self._toggle_pause,
        )
        self.pause_button.pack(side="left", padx=(0, 7))
        ttk.Button(
            controls,
            text="Stop",
            style="Danger.TButton",
            command=self._stop_run,
        ).pack(side="left")

    def _build_body(self) -> None:
        paned = tk.PanedWindow(
            self,
            orient="horizontal",
            bg=COLORS["bg"],
            sashwidth=5,
            sashrelief="flat",
            bd=0,
            opaqueresize=True,
        )
        paned.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        left = tk.Frame(paned, bg=COLORS["panel"], width=260)
        center = tk.Frame(paned, bg=COLORS["panel"], width=720)
        right = tk.Frame(paned, bg=COLORS["panel"], width=440)
        paned.add(left, minsize=230, width=260)
        paned.add(center, minsize=520, stretch="always")
        paned.add(right, minsize=330, width=430)
        self._build_left(left)
        self._build_center(center)
        self._build_right(right)

    @staticmethod
    def _section_title(parent: tk.Misc, title: str, subtitle: str = "") -> tk.Frame:
        header = tk.Frame(parent, bg=COLORS["panel"], padx=12, pady=10)
        tk.Label(
            header,
            text=title,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 13),
        ).pack(anchor="w")
        if subtitle:
            tk.Label(
                header,
                text=subtitle,
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                justify="left",
                wraplength=300,
            ).pack(anchor="w", pady=(2, 0))
        return header

    def _build_left(self, parent: tk.Frame) -> None:
        self._section_title(
            parent,
            "Role setup",
            "Select a worker card, then apply a preset or enter a custom role.",
        ).pack(fill="x")
        tk.Label(
            parent,
            textvariable=self.selected_text,
            bg=COLORS["panel_alt"],
            fg=COLORS["blue"],
            anchor="w",
            padx=10,
            pady=7,
        ).pack(fill="x", padx=12, pady=(0, 10))

        roles = tk.Frame(parent, bg=COLORS["panel"])
        roles.pack(fill="x", padx=12)
        for index, role in enumerate(("PLAN", "DEV", "REVIEW", "TEST", "SECURITY", "ARCHITECT")):
            ttk.Button(
                roles,
                text=role,
                style="Quiet.TButton",
                command=lambda value=role: self._apply_preset(value),
            ).grid(row=index // 2, column=index % 2, sticky="ew", padx=3, pady=3)
        roles.grid_columnconfigure(0, weight=1)
        roles.grid_columnconfigure(1, weight=1)
        ttk.Button(
            parent,
            text="+ Custom role",
            style="Quiet.TButton",
            command=self._custom_role,
        ).pack(fill="x", padx=15, pady=(6, 14))

        separator = tk.Frame(parent, bg=COLORS["border"], height=1)
        separator.pack(fill="x", padx=12, pady=(0, 8))
        self._section_title(parent, "Run settings").pack(fill="x")
        form = tk.Frame(parent, bg=COLORS["panel"], padx=12)
        form.pack(fill="x")
        self._field(form, "Task ID", ttk.Entry(form, textvariable=self.task_var, style="Studio.TEntry"), 0)
        self._field(
            form,
            "Rounds",
            ttk.Spinbox(form, from_=1, to=20, textvariable=self.rounds_var, width=8),
            1,
        )
        self._field(
            form,
            "Response timeout (seconds)",
            ttk.Spinbox(form, from_=10, to=3600, increment=10, textvariable=self.timeout_var),
            2,
        )
        self._field(
            form,
            "Context limit (characters)",
            ttk.Spinbox(form, from_=500, to=100000, increment=500, textvariable=self.context_var),
            3,
        )
        ttk.Button(
            parent,
            text="Save settings",
            style="Quiet.TButton",
            command=self._save_settings,
        ).pack(fill="x", padx=15, pady=12)

        status = tk.Frame(parent, bg=COLORS["panel_alt"], padx=11, pady=10)
        status.pack(side="bottom", fill="x", padx=12, pady=12)
        tk.Label(
            status,
            text="RUN STATUS",
            bg=COLORS["panel_alt"],
            fg=COLORS["muted"],
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w")
        self.run_status_label = tk.Label(
            status,
            textvariable=self.run_status_text,
            bg=COLORS["panel_alt"],
            fg=COLORS["accent"],
            font=("Segoe UI Semibold", 12),
        )
        self.run_status_label.pack(anchor="w", pady=(3, 2))
        tk.Label(
            status,
            textvariable=self.summary_text,
            bg=COLORS["panel_alt"],
            fg=COLORS["muted"],
            justify="left",
            wraplength=220,
        ).pack(anchor="w")

    @staticmethod
    def _field(parent: tk.Frame, label: str, widget: tk.Widget, row: int) -> None:
        tk.Label(
            parent,
            text=label,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
        ).grid(row=row * 2, column=0, sticky="ew", pady=(6, 3))
        widget.grid(row=row * 2 + 1, column=0, sticky="ew")
        parent.grid_columnconfigure(0, weight=1)

    def _build_center(self, parent: tk.Frame) -> None:
        header = self._section_title(
            parent,
            "Agent workflows",
            "Drag worker cards to change the next execution order. Active work has a green pulse.",
        )
        header.pack(fill="x", side="top")
        ttk.Button(
            header,
            text="Refresh tabs",
            style="Quiet.TButton",
            command=self._refresh_tabs,
        ).pack(side="right", anchor="e", pady=(6, 0))
        self.worker_list = ScrollableWorkerList(
            parent,
            on_select=self._select_worker,
            on_role_change=self._assign_role,
            on_enabled=self._set_enabled,
            on_edit_prompt=self._edit_prompt,
            on_view_response=self._view_response,
            on_stop=self._stop_worker,
            on_release=self._release_role,
            on_reorder=self._reorder_worker,
        )
        self.worker_list.pack(fill="both", expand=True, padx=4, pady=(0, 4))

    def _build_right(self, parent: tk.Frame) -> None:
        self._section_title(
            parent,
            "Agent logs & responses",
            "All events are mirrored here. Each worker receives its own log tab.",
        ).pack(fill="x")
        self.logs = LogNotebook(parent, max_lines=2500)
        self.logs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        system = tk.Frame(parent, bg=COLORS["panel_alt"], padx=11, pady=9)
        system.pack(fill="x", padx=10, pady=(0, 10))
        self.system_text = tk.StringVar(value="Waiting for connection")
        tk.Label(
            system,
            text="SYSTEM STATUS",
            bg=COLORS["panel_alt"],
            fg=COLORS["muted"],
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w")
        tk.Label(
            system,
            textvariable=self.system_text,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            justify="left",
            anchor="w",
            wraplength=390,
        ).pack(fill="x", pady=(4, 0))

    def _track(
        self,
        future: Future[Any],
        label: str,
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        self._pending.append(PendingFuture(future, label, on_success))

    def _poll(self) -> None:
        if self._closed:
            return
        for event in self.controller.poll_events():
            self._handle_event(event)
        remaining: list[PendingFuture] = []
        for pending in self._pending:
            if not pending.future.done():
                remaining.append(pending)
                continue
            try:
                result = pending.future.result()
            except Exception as exc:
                self.logs.append("System", f"ERROR {pending.label}: {type(exc).__name__}: {exc}")
                if pending.label == "connect":
                    self.connection_text.set("ERROR")
                    self.connection_dot.configure(fg=COLORS["red"])
            else:
                if pending.on_success is not None:
                    pending.on_success(result)
        self._pending = remaining
        self._render_state(self.controller.snapshot())
        self._poll_after = self.after(120, self._poll)

    def _handle_event(self, event: StudioEvent) -> None:
        try:
            stamp = datetime.fromisoformat(event.at).strftime("%H:%M:%S")
        except ValueError:
            stamp = "--:--:--"
        channel = event.role or "System"
        self.logs.append(channel, f"[{stamp}] {event.message}")
        if event.kind == "response" and event.page_id:
            state = self.controller.snapshot()
            worker = next((item for item in state.workers if item.page_id == event.page_id), None)
            if worker and worker.latest_response:
                self.logs.append(
                    channel,
                    f"\n--- RESPONSE {len(worker.responses)} ---\n{worker.latest_response}\n",
                )

    def _render_state(self, state: StudioState, *, force: bool = False) -> None:
        encoded = json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True)
        if not force and encoded == self._last_state_json:
            return
        self._last_state_json = encoded
        self.worker_list.set_workers(state.workers)
        for worker in state.workers:
            if worker.role:
                self.logs.ensure_channel(worker.role)
        if not self._settings_loaded:
            self.task_var.set(state.settings.task_id)
            self.rounds_var.set(state.settings.rounds)
            self.timeout_var.set(max(state.settings.response_timeout_ms // 1000, 10))
            self.context_var.set(state.settings.context_char_limit)
            if state.settings.global_goal:
                self.goal_text.insert("1.0", state.settings.global_goal)
            self._settings_loaded = True
        self.run_status_text.set(state.run_status.value.upper())
        self.run_status_label.configure(
            fg=STATUS_COLORS.get(state.run_status.value, COLORS["muted"])
        )
        enabled = [item for item in state.workers if item.enabled]
        completed = [item for item in enabled if item.status is WorkerStatus.COMPLETED]
        self.summary_text.set(
            f"{len(state.workers)} tabs · {len(enabled)} selected · {len(completed)} completed"
        )
        active = next(
            (item for item in state.workers if item.page_id == state.active_page_id),
            None,
        )
        self.system_text.set(
            f"Connection: {self.connection_text.get()}\n"
            f"Task: {state.settings.task_id or 'not set'}\n"
            f"Round: {state.current_round + 1}/{state.settings.rounds}\n"
            f"Active: {active.display_role if active else 'none'}"
        )
        if state.run_status is RunStatus.PAUSED:
            self.pause_button.configure(text="Resume")
        else:
            self.pause_button.configure(text="Pause")
        if self._selected_page_id:
            selected = next(
                (item for item in state.workers if item.page_id == self._selected_page_id),
                None,
            )
            if selected:
                self.selected_text.set(
                    f"{selected.display_role} · {selected.title[:35]} · {selected.page_id[:8]}"
                )
        elif state.workers:
            self._select_worker(state.workers[0].page_id)

    def _connect(self) -> None:
        self.connection_text.set("CONNECTING")
        self.connection_dot.configure(fg=COLORS["amber"])

        def connected(_result: Any) -> None:
            self.connection_text.set("CONNECTED")
            self.connection_dot.configure(fg=COLORS["accent"])
            self.logs.append("System", "Connected to CDP and discovered ChatGPT tabs")

        self._track(self.controller.connect(), "connect", connected)

    def _refresh_tabs(self) -> None:
        self._track(self.controller.refresh_tabs(), "refresh tabs")

    def _select_worker(self, page_id: str) -> None:
        self._selected_page_id = page_id
        self.worker_list.selected_page_id = page_id
        state = self.controller.snapshot()
        worker = next((item for item in state.workers if item.page_id == page_id), None)
        if worker:
            self.selected_text.set(
                f"{worker.display_role} · {worker.title[:35]} · {worker.page_id[:8]}"
            )
            self.worker_list.set_workers(state.workers)

    def _apply_preset(self, role: str) -> None:
        if not self._selected_page_id:
            messagebox.showinfo("Select worker", "Select a worker card first.", parent=self)
            return
        self._assign_role(self._selected_page_id, role)

    def _custom_role(self) -> None:
        if not self._selected_page_id:
            messagebox.showinfo("Select worker", "Select a worker card first.", parent=self)
            return
        role = simpledialog.askstring("Custom role", "Role name:", parent=self)
        if role:
            self._assign_role(self._selected_page_id, role.strip())

    def _worker_by_id(self, page_id: str) -> WorkerModel | None:
        return next(
            (item for item in self.controller.snapshot().workers if item.page_id == page_id),
            None,
        )

    @staticmethod
    def _is_active(worker: WorkerModel | None) -> bool:
        return bool(
            worker
            and worker.status
            in {
                WorkerStatus.PREPARING,
                WorkerStatus.RUNNING,
                WorkerStatus.WAITING,
                WorkerStatus.STOPPING,
            }
        )

    def _assign_role(self, page_id: str, role: str) -> None:
        worker = self._worker_by_id(page_id)
        if self._is_active(worker) and worker and worker.role != role:
            if not messagebox.askyesno(
                "Active worker",
                "This tab is active. Stop the current response before changing its role?",
                parent=self,
            ):
                self._render_state(self.controller.snapshot(), force=True)
                return
            self._track(
                self.controller.stop_worker(page_id),
                "stop active worker",
                lambda _result: self._track(
                    self.controller.assign_role(page_id, role), "assign role"
                ),
            )
            return
        self._track(self.controller.assign_role(page_id, role), "assign role")

    def _release_role(self, page_id: str) -> None:
        worker = self._worker_by_id(page_id)
        if self._is_active(worker):
            if not messagebox.askyesno(
                "Active worker",
                "Stop the active response and release this role?",
                parent=self,
            ):
                return
            self._track(
                self.controller.stop_worker(page_id),
                "stop active worker",
                lambda _result: self._track(
                    self.controller.release_role(page_id), "release role"
                ),
            )
            return
        self._track(self.controller.release_role(page_id), "release role")

    def _set_enabled(self, page_id: str, enabled: bool) -> None:
        self._track(self.controller.set_enabled(page_id, enabled), "select worker")

    def _reorder_worker(self, page_id: str, target_index: int) -> None:
        self._track(self.controller.reorder(page_id, target_index), "reorder worker")

    def _edit_prompt(self, page_id: str) -> None:
        worker = self._worker_by_id(page_id)
        if worker is None:
            return
        PromptDialog(
            self,
            worker,
            lambda prompt, retry: self._track(
                self.controller.update_prompt(
                    page_id,
                    prompt,
                    stop_and_retry=retry,
                ),
                "update prompt",
            ),
        )

    def _view_response(self, page_id: str) -> None:
        worker = self._worker_by_id(page_id)
        if worker is not None:
            ResponseDialog(self, worker)

    def _stop_worker(self, page_id: str) -> None:
        self._track(self.controller.stop_worker(page_id), "stop worker")

    def _collect_settings(self) -> StudioSettings:
        goal = self.goal_text.get("1.0", "end-1c").strip()
        settings = StudioSettings(
            global_goal=goal,
            task_id=self.task_var.get().strip(),
            rounds=int(self.rounds_var.get()),
            response_timeout_ms=int(self.timeout_var.get()) * 1000,
            context_char_limit=int(self.context_var.get()),
            cdp_url=self.controller.snapshot().settings.cdp_url,
        )
        settings.validate()
        return settings

    def _save_settings(self) -> None:
        try:
            settings = self._collect_settings()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self)
            return
        self._track(self.controller.update_settings(settings), "save settings")

    def _start_run(self) -> None:
        try:
            settings = self._collect_settings()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Cannot start", str(exc), parent=self)
            return

        def start_after_settings(_result: Any) -> None:
            self._track(self.controller.start_run(), "start collaborative task")

        self._track(
            self.controller.update_settings(settings),
            "save run settings",
            start_after_settings,
        )

    def _toggle_pause(self) -> None:
        state = self.controller.snapshot()
        if state.run_status in {RunStatus.PAUSED, RunStatus.PAUSING}:
            self._track(self.controller.resume_run(), "resume run")
        else:
            self._track(self.controller.pause_run(), "pause run")

    def _stop_run(self) -> None:
        self._track(self.controller.stop_run(), "stop run")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._poll_after is not None:
            try:
                self.after_cancel(self._poll_after)
            except tk.TclError:
                pass
            self._poll_after = None
        self.controller.shutdown()
        if self.winfo_exists():
            self.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tkinter control studio for persistent ChatGPT tabs on CDP 9222"
    )
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--runtime-dir", default=".runtime/studio")
    parser.add_argument("--geometry", default="1480x900")
    parser.add_argument("--no-auto-connect", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = tk.Tk()
    root.title("AI Multi-Agent Studio")
    root.geometry(args.geometry)
    root.minsize(1120, 700)
    root.configure(bg=COLORS["bg"])
    controller = StudioController(runtime_dir=Path(args.runtime_dir))
    state = controller.snapshot()
    state.settings.cdp_url = args.cdp
    controller.update_settings(state.settings).result(timeout=5)
    app = StudioApp(root, controller=controller, auto_connect=not args.no_auto_connect)
    app.pack(fill="both", expand=True)

    def close() -> None:
        app.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
