from __future__ import annotations

import json
import os
import queue
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from bci_store import BCIStore
from bci_updater import (
    GitHubUpdateClient,
    UpdateCheckResult,
    UpdateSettings,
    compare_versions,
    is_check_due,
    is_installer_managed_install,
    launch_verified_installer,
    load_update_settings,
    save_update_settings,
)
from bci_ml import SCENARIOS, export_active_model_package, import_model_package, predict_report, train_model

from bci_core import (
    APP_VERSION,
    baseline_directory,
    compare_with_baseline,
    export_report,
    export_load_order,
    format_current_order_status,
    resolve_game_paths,
    save_baseline,
    scan_modules,
)


class BCIApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Bannerlord Compatibility Intelligence — {APP_VERSION}")
        self.geometry("1360x850")
        self.minsize(1060, 700)
        self.configure(bg="#10151d")

        self.game_var = tk.StringVar()
        self.profile_path_var = tk.StringVar(value="")
        self.force_rehash_var = tk.BooleanVar(value=False)
        self.game_detect_var = tk.StringVar(value="Выбери папку игры — ту, где находятся Modules и bin.")
        self.profile_status_var = tk.StringVar(value="BCI проверит, какие моды включены. Если данных недостаточно, он не будет угадывать.")
        self.status_var = tk.StringVar(value="Сначала выбери папку игры.")
        self.summary_var = tk.StringVar(value="После проверки здесь появится краткая сводка сборки.")
        self.order_summary_var = tk.StringVar(value="BCI покажет собственную рекомендацию порядка загрузки.")
        self.order_evidence_var = tk.StringVar(value="Процент будет показывать покрытие парами явных правил, а не вероятность совместимости.")
        self.report = None
        self.report_dir: Path | None = None
        self.latest_html: Path | None = None
        self.latest_json: Path | None = None
        self.progress_queue: queue.Queue = queue.Queue()
        self.scanning = False
        self.progress_window: tk.Toplevel | None = None
        self.progress_bar: ttk.Progressbar | None = None
        self.progress_label: ttk.Label | None = None
        self.progress_detail: ttk.Label | None = None
        self.progress_log: tk.Text | None = None
        self.progress_log_toggle: ttk.Button | None = None
        self._progress_log_visible = False
        self._last_progress_line = ""
        self.findings_by_id = {}
        self.store = BCIStore()
        self.update_client = GitHubUpdateClient()
        self.installer_managed = is_installer_managed_install()
        self.update_settings_path = self.store.root / "update_settings.json"
        default_channel = "Preview" if "-" in APP_VERSION else "Stable"
        self.update_settings = load_update_settings(self.update_settings_path, default_channel=default_channel)
        self.update_channel_var = tk.StringVar(value=self.update_settings.channel)
        self.update_auto_check_var = tk.BooleanVar(value=self.update_settings.auto_check)
        self.update_status_var = tk.StringVar(value="Проверка обновлений ещё не выполнялась.")
        self.update_version_var = tk.StringVar(value=f"Установлена версия BCI {APP_VERSION}")
        self.update_detail_var = tk.StringVar(value="BCI будет проверять официальный GitHub Releases. Обновления устанавливаются только после подтверждения Windows.")
        self.update_queue: queue.Queue = queue.Queue()
        self.update_check_result: UpdateCheckResult | None = None
        self.update_busy = False
        self.update_notes_text: tk.Text | None = None
        self.update_check_btn: ttk.Button | None = None
        self.update_install_btn: ttk.Button | None = None
        self.update_progress: ttk.Progressbar | None = None
        self.current_scan_id: str | None = None
        self.training_queue: queue.Queue = queue.Queue()
        self.training_in_progress = False
        self.training_status_var = tk.StringVar(value="Локальная база готова. Отчёты будут сохраняться автоматически.")
        self.training_stats_var = tk.StringVar(value="")
        self.model_status_var = tk.StringVar(value="Обученной ML-модели пока нет. Встроенный статический анализатор уже работает; для обучения нужны подтверждённые результаты игровых тестов.")

        self._style()
        self._build()
        self.after(250, self._poll_update_queue)
        self.after(1800, self._auto_check_updates)

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#10151d")
        style.configure("TLabel", background="#10151d", foreground="#e8edf4", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 22, "bold"), foreground="#f6c94a")
        style.configure("Sub.TLabel", foreground="#aab6c5", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10), padding=8)
        style.configure("Accent.TButton", background="#e0b82f", foreground="#111111", font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#f0ca4b")])
        style.configure("Treeview", background="#19222e", fieldbackground="#19222e", foreground="#e8edf4", rowheight=28)
        style.configure("Treeview.Heading", background="#263344", foreground="#f6c94a", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#36516b")])
        style.configure("TNotebook", background="#10151d", borderwidth=0)
        style.configure("TNotebook.Tab", background="#263344", foreground="#e8edf4", padding=(12, 8))
        style.map("TNotebook.Tab", background=[("selected", "#e0b82f")], foreground=[("selected", "#111111")])
        style.configure("TProgressbar", troughcolor="#263344", background="#e0b82f", thickness=12)

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="BCI", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Bannerlord Compatibility Intelligence · Проверка совместимости модов",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(0, 14))

        paths = ttk.Frame(outer)
        paths.pack(fill="x", pady=(0, 10))
        ttk.Label(paths, text="Корневая папка игры:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(paths, textvariable=self.game_var).grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(paths, text="Выбрать папку игры…", command=self._choose_game).grid(row=0, column=2, pady=4)
        paths.columnconfigure(1, weight=1)
        ttk.Label(paths, textvariable=self.game_detect_var, style="Sub.TLabel", wraplength=1250).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 5)
        )
        ttk.Label(paths, text="Профиль Vortex / BEM (ручной JSON — рекомендуется):").grid(
            row=2, column=0, sticky="w", pady=4
        )
        ttk.Entry(paths, textvariable=self.profile_path_var).grid(
            row=2, column=1, sticky="ew", padx=8, pady=4
        )
        profile_actions = ttk.Frame(paths)
        profile_actions.grid(row=2, column=2, sticky="e", pady=4)
        ttk.Button(profile_actions, text="Выбрать JSON профиля…", style="Accent.TButton", command=self._choose_profile).pack(side="left")
        ttk.Button(profile_actions, text="Сбросить (автопоиск)", command=lambda: self.profile_path_var.set("")).pack(side="left", padx=(6, 0))
        ttk.Label(
            paths,
            text=r"Vortex сохраняет JSON порядка профиля в %APPDATA%\Vortex\mountandblade2bannerlord\mods\<profileId>_loadorder.json (Singleplayer) или с суффиксом _multiplayer.json. Если поле пустое, BCI сначала ищет свежий снимок состояния Vortex, затем профильный JSON, однозначно выделяющийся по времени изменения (не старше 30 минут). Такой выбор по времени помечается как предположительный, а не подтверждённый. При неоднозначности BCI не угадывает; можно выбрать JSON вручную. Остаточные настройки BEM не перекрывают найденный Vortex-профиль. Порядок Vortex используется только для определения состава и аудита, собственную сортировку BCI строит независимо.",
            style="Sub.TLabel", wraplength=1250,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 5))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 10))
        self.scan_btn = ttk.Button(actions, text="Проверить моды", style="Accent.TButton", command=self._scan)
        self.scan_btn.pack(side="left")
        self.baseline_btn = ttk.Button(actions, text="Сохранить контрольную сборку", command=self._save_baseline, state="disabled")
        self.baseline_btn.pack(side="left", padx=8)
        self.export_btn = ttk.Button(actions, text="Открыть HTML-отчёт", command=self._open_report, state="disabled")
        self.export_btn.pack(side="left")
        ttk.Checkbutton(
            actions, text="Полная перепроверка файлов (без кеша)",
            variable=self.force_rehash_var,
        ).pack(side="left", padx=(12, 0))
        ttk.Button(actions, text="Как пользоваться", command=self._help).pack(side="right")

        self.summary_frame = ttk.Frame(outer)
        self.summary_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(self.summary_frame, textvariable=self.profile_status_var, wraplength=1250).pack(anchor="w", pady=(0, 4))
        self.summary_label = ttk.Label(self.summary_frame, textvariable=self.summary_var, style="Sub.TLabel", wraplength=1250)
        self.summary_label.pack(anchor="w")

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        rank_tab = ttk.Frame(notebook, padding=8)
        finding_tab = ttk.Frame(notebook, padding=8)
        order_tab = ttk.Frame(notebook, padding=8)
        module_tab = ttk.Frame(notebook, padding=8)
        training_tab = ttk.Frame(notebook, padding=8)
        update_tab = ttk.Frame(notebook, padding=16)
        notebook.add(rank_tab, text="Приоритет проверки")
        notebook.add(finding_tab, text="Найденные взаимодействия")
        notebook.add(order_tab, text="Порядок загрузки")
        notebook.add(module_tab, text="Установленные моды")
        notebook.add(training_tab, text="История и обучение")
        notebook.add(update_tab, text="Обновления")

        self._build_update_tab(update_tab)

        self.ranking_tree = self._tree(
            rank_tab,
            ("rank", "candidate", "name", "module_id", "risk", "impact", "count", "related"),
            (45, 175, 210, 190, 110, 130, 85, 300),
        )
        self.finding_tree = self._tree(
            finding_tab,
            ("severity", "category", "title", "modules", "points", "confidence"),
            (100, 145, 420, 280, 85, 110),
        )
        self.finding_tree.bind("<Double-1>", self._show_finding)

        order_frame = ttk.Frame(order_tab)
        order_frame.pack(fill="both", expand=True)
        ttk.Label(order_frame, textvariable=self.order_summary_var, style="Sub.TLabel", wraplength=1200).pack(anchor="w", pady=(0, 5))
        ttk.Label(order_frame, textvariable=self.order_evidence_var, style="Sub.TLabel", wraplength=1200).pack(anchor="w", pady=(0, 7))
        order_actions = ttk.Frame(order_frame)
        order_actions.pack(fill="x", pady=(0, 7))
        self.order_export_btn = ttk.Button(
            order_actions, text="Экспортировать порядок…", command=self._export_load_order, state="disabled"
        )
        self.order_export_btn.pack(side="left")
        ttk.Label(
            order_actions,
            text="Текстовый список для ручной перестановки в Vortex; настройки Vortex не изменяются.",
            style="Sub.TLabel",
        ).pack(side="left", padx=10)
        self.order_views = ttk.Notebook(order_frame)
        self.order_views.pack(fill="both", expand=True)
        order_list_tab = ttk.Frame(self.order_views, padding=4)
        order_pairs_tab = ttk.Frame(self.order_views, padding=4)
        self.order_views.add(order_list_tab, text="Рекомендуемый порядок")
        self.order_views.add(order_pairs_tab, text="Файловые взаимодействия между модами")
        self.order_tree = self._tree(
            order_list_tab,
            ("recommended", "group", "current", "name", "module_id", "coverage", "reason"),
            (80, 75, 80, 190, 190, 115, 470),
        )
        self.order_pairs_tree = self._tree(
            order_pairs_tab,
            ("pair_modules", "xml_conflicts", "resource_paths", "evidence", "direction", "conclusion"),
            (210, 90, 100, 125, 140, 520),
        )

        self.module_tree = self._tree(
            module_tab,
            ("enabled", "name", "module_id", "version", "official", "order", "folder"),
            (90, 240, 250, 120, 100, 100, 300),
        )

        training_head = ttk.Frame(training_tab)
        training_head.pack(fill="x", pady=(0, 8))
        ttk.Label(
            training_head,
            text="Отчёты и результаты хранятся только на этом компьютере. BCI ничего не отправляет в сеть.",
            style="Sub.TLabel", wraplength=1160,
        ).pack(anchor="w", pady=(0, 8))
        ttk.Label(training_head, textvariable=self.training_stats_var, style="Sub.TLabel", wraplength=1160).pack(anchor="w")
        ttk.Label(training_head, textvariable=self.model_status_var, style="Sub.TLabel", wraplength=1160).pack(anchor="w", pady=(4, 8))
        training_actions = ttk.Frame(training_tab)
        training_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(training_actions, text="Добавить результат игрового теста…", command=self._label_selected_scan).pack(side="left")
        self.train_btn = ttk.Button(training_actions, text="Обучить экспериментальную модель", command=self._train_model)
        self.train_btn.pack(side="left", padx=8)
        ttk.Button(training_actions, text="Импортировать модель…", command=self._import_model).pack(side="left", padx=(0, 8))
        ttk.Button(training_actions, text="Экспортировать модель…", command=self._export_model).pack(side="left")
        ttk.Button(training_actions, text="Открыть папку данных", command=self._open_data_folder).pack(side="left", padx=8)
        ttk.Button(training_actions, text="Обновить список", command=self._refresh_training_view).pack(side="right")
        self.scan_history_tree = self._tree(
            training_tab,
            ("date", "modules", "findings", "scenario", "outcome"),
            (180, 100, 100, 230, 250),
        )
        self.scan_history_tree.heading("date", text="Дата отчёта")
        self.scan_history_tree.heading("modules", text="Модулей")
        self.scan_history_tree.heading("findings", text="Находок")
        self.scan_history_tree.heading("scenario", text="Сценарий теста")
        self.scan_history_tree.heading("outcome", text="Результат")
        ttk.Label(training_tab, textvariable=self.training_status_var, style="Sub.TLabel", wraplength=1160).pack(fill="x", pady=(8, 0))

        ttk.Label(outer, textvariable=self.status_var, style="Sub.TLabel", wraplength=1250).pack(fill="x", pady=(10, 0))
        self._refresh_training_view()

    def _build_update_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Центр обновлений BCI", style="Header.TLabel").pack(anchor="w")
        ttk.Label(parent, textvariable=self.update_version_var, style="Sub.TLabel").pack(anchor="w", pady=(4, 12))

        settings = ttk.Frame(parent)
        settings.pack(fill="x", pady=(0, 12))
        ttk.Label(settings, text="Канал:").pack(side="left")
        channel_box = ttk.Combobox(
            settings, textvariable=self.update_channel_var,
            values=("Stable", "Preview"), state="readonly", width=12,
        )
        channel_box.pack(side="left", padx=(8, 16))
        channel_box.bind("<<ComboboxSelected>>", self._on_update_channel_changed)
        ttk.Checkbutton(
            settings, text="Автоматически проверять обновления раз в сутки",
            variable=self.update_auto_check_var, command=self._save_update_preferences,
        ).pack(side="left")

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(0, 12))
        self.update_check_btn = ttk.Button(
            actions, text="Проверить обновления", style="Accent.TButton",
            command=lambda: self._check_updates(manual=True),
        )
        self.update_check_btn.pack(side="left")
        self.update_install_btn = ttk.Button(
            actions, text="Скачать и установить", command=self._install_update, state="disabled",
        )
        self.update_install_btn.pack(side="left", padx=8)
        ttk.Button(
            actions, text="Открыть GitHub Releases", command=lambda: webbrowser.open("https://github.com/VoodyWoodman/BCI/releases"),
        ).pack(side="right")

        ttk.Label(parent, textvariable=self.update_status_var, wraplength=1180).pack(anchor="w", pady=(0, 5))
        ttk.Label(parent, textvariable=self.update_detail_var, style="Sub.TLabel", wraplength=1180).pack(anchor="w", pady=(0, 10))
        self.update_progress = ttk.Progressbar(parent, mode="indeterminate")
        self.update_progress.pack(fill="x", pady=(0, 10))
        self.update_progress.stop()

        ttk.Label(parent, text="Список изменений последнего подходящего релиза:").pack(anchor="w", pady=(0, 4))
        notes_frame = ttk.Frame(parent)
        notes_frame.pack(fill="both", expand=True)
        self.update_notes_text = tk.Text(
            notes_frame, height=12, wrap="word", bg="#19222e", fg="#e8edf4",
            insertbackground="white", relief="flat", padx=10, pady=8,
        )
        notes_scroll = ttk.Scrollbar(notes_frame, orient="vertical", command=self.update_notes_text.yview)
        self.update_notes_text.configure(yscrollcommand=notes_scroll.set, state="disabled")
        self.update_notes_text.pack(side="left", fill="both", expand=True)
        notes_scroll.pack(side="right", fill="y")
        self._set_update_notes("Информация о релизе появится после проверки GitHub.")
        if not self.installer_managed:
            self.update_detail_var.set(
                "Автоустановка обновлений доступна в версии, установленной через BCI Setup. "
                "Portable- и исходная версии могут проверять релизы, но должны обновляться вручную."
            )

    def _set_update_notes(self, value: str) -> None:
        if self.update_notes_text is None:
            return
        self.update_notes_text.configure(state="normal")
        self.update_notes_text.delete("1.0", "end")
        self.update_notes_text.insert("1.0", (value or "Нет описания изменений.")[:12000])
        self.update_notes_text.configure(state="disabled")

    def _save_update_preferences(self, *, last_check_utc: str | None = None) -> None:
        previous_last_check = self.update_settings.last_check_utc
        self.update_settings = UpdateSettings(
            channel=self.update_channel_var.get(),
            auto_check=bool(self.update_auto_check_var.get()),
            last_check_utc=last_check_utc if last_check_utc is not None else previous_last_check,
        )
        save_update_settings(self.update_settings_path, self.update_settings)

    def _on_update_channel_changed(self, _event=None) -> None:
        self._save_update_preferences()
        self._check_updates(manual=True)

    def _auto_check_updates(self) -> None:
        if self.installer_managed and self.update_auto_check_var.get() and is_check_due(self.update_settings.last_check_utc):
            self._check_updates(manual=False)
        self.after(24 * 60 * 60 * 1000, self._auto_check_updates)

    def _check_updates(self, *, manual: bool) -> None:
        if self.update_busy:
            return
        self._save_update_preferences()
        self.update_busy = True
        if self.update_check_btn is not None:
            self.update_check_btn.configure(state="disabled")
        if self.update_install_btn is not None:
            self.update_install_btn.configure(state="disabled")
        self.update_status_var.set("Проверяем официальный GitHub Releases…")
        self.update_detail_var.set("Проверка выполняется в фоне; работа анализатора модов не блокируется.")

        selected_channel = self.update_channel_var.get()

        def worker() -> None:
            try:
                result = self.update_client.check_for_updates(APP_VERSION, selected_channel)
                checked_at = datetime.now(timezone.utc).isoformat()
                self.update_queue.put({"type": "check_done", "result": result, "checked_at": checked_at, "manual": manual})
            except Exception as exc:
                self.update_queue.put({"type": "check_error", "error": f"{type(exc).__name__}: {exc}", "manual": manual})

        threading.Thread(target=worker, name="BCI-Update-Check", daemon=True).start()

    def _install_update(self) -> None:
        result = self.update_check_result
        if self.update_busy or result is None or not result.installer_ready:
            return
        if not self.installer_managed:
            messagebox.showinfo(
                "BCI — обновления",
                "Автоматическая установка доступна только для версии, установленной через BCI Setup. "
                "Скачай новый установщик с GitHub Releases и запусти его вручную.",
            )
            return
        accepted = messagebox.askyesno(
            "BCI — установка обновления",
            f"Доступна BCI {result.latest_version}.\n\n"
            "BCI загрузит установщик с официального GitHub Releases, проверит SHA-256 и запустит установку.\n"
            "Приложение закроется. Windows запросит подтверждение UAC для установки в Program Files.\n\n"
            "Продолжить?",
        )
        if not accepted:
            return
        self.update_busy = True
        if self.update_check_btn is not None:
            self.update_check_btn.configure(state="disabled")
        if self.update_install_btn is not None:
            self.update_install_btn.configure(state="disabled")
        if self.update_progress is not None:
            self.update_progress.configure(mode="indeterminate")
            self.update_progress.start(12)
        self.update_status_var.set(f"Загружаем BCI {result.latest_version}…")
        self.update_detail_var.set("После загрузки BCI сверит размер и SHA-256 установщика с манифестом релиза.")

        destination = self.store.root / "Updates"

        def worker() -> None:
            try:
                installer = self.update_client.download_and_verify(result, destination)
                self.update_queue.put({"type": "download_done", "path": str(installer), "version": result.latest_version})
            except Exception as exc:
                self.update_queue.put({"type": "download_error", "error": f"{type(exc).__name__}: {exc}"})

        threading.Thread(target=worker, name="BCI-Update-Download", daemon=True).start()

    def _poll_update_queue(self) -> None:
        while True:
            try:
                item = self.update_queue.get_nowait()
            except queue.Empty:
                break
            kind = item.get("type")
            if kind == "check_done":
                self.update_busy = False
                self.update_check_result = item["result"]
                self._save_update_preferences(last_check_utc=item["checked_at"])
                result = self.update_check_result
                if result.latest_version is None:
                    self.update_status_var.set("Для выбранного канала пока нет опубликованных релизов.")
                    self.update_detail_var.set("Проверь канал Preview/Stable или открой GitHub Releases.")
                    self._set_update_notes("Релизы выбранного канала не найдены.")
                elif not result.update_available:
                    if compare_versions(APP_VERSION, result.latest_version) > 0:
                        self.update_status_var.set("Установлена сборка новее последнего опубликованного релиза GitHub.")
                        self.update_detail_var.set(
                            f"Текущая версия: {APP_VERSION}; последний релиз канала: {result.latest_version}. "
                            "Для проверки обновлений после этой сборки дождись публикации следующего GitHub Release."
                        )
                    else:
                        self.update_status_var.set(f"Установлена актуальная версия BCI {APP_VERSION}.")
                        self.update_detail_var.set(f"Последний подходящий релиз: {result.latest_version}. Проверка выполнена {item['checked_at'][:19]} UTC.")
                    self._set_update_notes(result.release_notes)
                elif result.installer_ready:
                    self.update_status_var.set(f"Доступно обновление BCI {result.latest_version}.")
                    self.update_detail_var.set(
                        f"Канал: {'Preview' if result.prerelease else 'Stable'} · опубликовано: {result.published_at or 'не указано'}. "
                        "Установщик будет проверен по размеру и SHA-256 перед запуском."
                    )
                    if self.update_install_btn is not None and self.installer_managed:
                        self.update_install_btn.configure(state="normal")
                    if not self.installer_managed:
                        self.update_detail_var.set(
                            "Для автоматической установки нужно установить BCI через официальный Setup.exe. "
                            "Эта portable/source-копия может только показать доступный релиз."
                        )
                    self._set_update_notes(result.release_notes)
                else:
                    self.update_status_var.set(f"Найден релиз BCI {result.latest_version}, но автоустановка недоступна.")
                    self.update_detail_var.set(result.error or "В релизе нет проверенного установщика и манифеста.")
                    self._set_update_notes(result.release_notes)
                if item.get("manual") and result.error:
                    messagebox.showwarning("BCI — обновления", result.error)
            elif kind == "check_error":
                self.update_busy = False
                self.update_status_var.set("Не удалось проверить обновления. BCI продолжает работать в текущей версии.")
                self.update_detail_var.set(item.get("error", "Неизвестная ошибка проверки GitHub."))
                if item.get("manual"):
                    messagebox.showwarning("BCI — проверка обновлений", item.get("error", "Неизвестная ошибка."))
            elif kind == "download_done":
                if self.update_progress is not None:
                    self.update_progress.stop()
                self.update_busy = False
                self.update_status_var.set("Установщик загружен и SHA-256 проверен. Запускаем Windows Installer…")
                self.update_detail_var.set(f"Файл: {item['path']}")
                try:
                    launch_verified_installer(item["path"])
                except Exception as exc:
                    if self.update_check_btn is not None:
                        self.update_check_btn.configure(state="normal")
                    if self.update_install_btn is not None and self.update_check_result and self.update_check_result.installer_ready:
                        self.update_install_btn.configure(state="normal")
                    messagebox.showerror("BCI — запуск установщика", str(exc))
                    continue
                self.destroy()
                return
            elif kind == "download_error":
                if self.update_progress is not None:
                    self.update_progress.stop()
                self.update_busy = False
                if self.update_check_btn is not None:
                    self.update_check_btn.configure(state="normal")
                self.update_status_var.set("Обновление не установлено. Текущая версия BCI сохранена.")
                self.update_detail_var.set(item.get("error", "Неизвестная ошибка загрузки."))
                messagebox.showerror("BCI — обновление", item.get("error", "Неизвестная ошибка."))
        if not self.update_busy and self.update_check_btn is not None:
            self.update_check_btn.configure(state="normal")
        self.after(250, self._poll_update_queue)

    def _tree(self, parent, columns, widths):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        headings = {
            "rank": "#", "candidate": "Изменение относительно базы", "name": "Название", "module_id": "ID мода", "risk": "Приоритет (баллы)",
            "impact": "Влияние изменения", "count": "Находки", "related": "Связанные моды", "severity": "Уровень", "category": "Категория",
            "title": "Описание", "modules": "Моды", "points": "Баллы риска", "confidence": "Уверенность",
            "enabled": "Состояние", "version": "Версия", "official": "Официальный", "order": "Текущая позиция",
            "recommended": "Рекомендуемая позиция", "group": "Группа", "current": "Сейчас", "kind": "Тип", "reason": "Почему здесь", "folder": "Папка",
            "coverage": "Покрытие пар", "pair_modules": "Пара модов", "xml_conflicts": "XML-поля", "resource_paths": "Ресурсы", "evidence": "Качество данных", "direction": "Направление", "conclusion": "Вывод BCI",
        }
        for col, width in zip(columns, widths):
            tree.heading(col, text=headings.get(col, col))
            tree.column(col, width=width, minwidth=70, anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _choose_game(self) -> None:
        path = filedialog.askdirectory(title="Выберите корневую папку Mount & Blade II Bannerlord")
        if not path:
            return
        self.game_var.set(path)
        self._preview_game_path(path)

    def _choose_profile(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите JSON порядка/профиля Vortex или BEM",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")],
        )
        if path:
            self.profile_path_var.set(path)

    def _preview_game_path(self, path: str) -> None:
        try:
            resolved = resolve_game_paths(path)
            self.game_var.set(str(resolved.game_root))
            suffix = " (выбрана папка Modules — путь нормализован)" if resolved.selected_was_modules else ""
            self.game_detect_var.set(f"Папка игры найдена{suffix}. Модули обнаружены автоматически.")
            self.profile_status_var.set("Рекомендуется выбрать JSON активного профиля Vortex вручную. Автоопределение по свежему снимку Vortex — дополнительный способ.")
            self.status_var.set("Всё готово к проверке модов.")
        except Exception as exc:
            self.game_detect_var.set(str(exc))
            self.profile_status_var.set("Профиль Vortex/BEM/Launcher нельзя проверить, пока не распознана папка игры.")
            self.status_var.set("Папка игры пока не распознана.")

    def _show_progress(self) -> None:
        win = tk.Toplevel(self)
        win.title("BCI — анализ сборки")
        win.geometry("660x390")
        win.minsize(560, 330)
        win.configure(bg="#10151d")
        win.transient(self)
        self.progress_window = win
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="BCI выполняет анализ", style="Header.TLabel").pack(anchor="w")
        self.progress_label = ttk.Label(frame, text="Подготовка…", style="Sub.TLabel")
        self.progress_label.pack(anchor="w", pady=(12, 4))
        self.progress_bar = ttk.Progressbar(frame, mode="determinate", maximum=100, value=0)
        self.progress_bar.pack(fill="x", pady=8)
        self.progress_detail = ttk.Label(frame, text="", style="Sub.TLabel", wraplength=610)
        self.progress_detail.pack(anchor="w", pady=(0, 8))
        self.progress_log_toggle = ttk.Button(frame, text="Показать технические подробности", command=self._toggle_progress_log)
        self.progress_log_toggle.pack(anchor="w", pady=(6, 4))
        self.progress_log = tk.Text(frame, height=8, wrap="word", bg="#19222e", fg="#dce5ef", insertbackground="white", relief="flat")
        self.progress_log.configure(state="disabled")
        self._progress_log_visible = False
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        win.update_idletasks()

    def _toggle_progress_log(self) -> None:
        if self.progress_log is None:
            return
        if self._progress_log_visible:
            self.progress_log.pack_forget()
            self._progress_log_visible = False
            label = "Показать технические подробности"
        else:
            self.progress_log.pack(fill="both", expand=True)
            self._progress_log_visible = True
            label = "Скрыть технические подробности"
        if self.progress_log_toggle is not None:
            self.progress_log_toggle.configure(text=label)

    def _log_progress(self, line: str) -> None:
        if self.progress_log is None or not line or line == self._last_progress_line:
            return
        self._last_progress_line = line
        self.progress_log.configure(state="normal")
        self.progress_log.insert("end", line + "\n")
        self.progress_log.see("end")
        self.progress_log.configure(state="disabled")

    def _scan(self) -> None:
        if self.scanning:
            return
        selected = self.game_var.get().strip()
        if not selected:
            messagebox.showwarning("BCI", "Сначала выбери корневую папку игры Bannerlord.")
            return
        try:
            game_paths = resolve_game_paths(selected)
        except Exception as exc:
            messagebox.showerror("BCI — папка игры не распознана", str(exc))
            return

        self.game_var.set(str(game_paths.game_root))
        self.game_detect_var.set(f"Папка игры выбрана: {game_paths.game_root}")
        selected_profile = self.profile_path_var.get().strip() or None
        if selected_profile and not Path(selected_profile).is_file():
            messagebox.showerror("BCI — профиль не найден", f"Выбранный JSON не существует:\n{selected_profile}")
            return
        force_rehash = bool(self.force_rehash_var.get())
        self.profile_status_var.set("Проверяем активный профиль Vortex/BEM/Launcher…")
        self.scanning = True
        self.scan_btn.configure(state="disabled")
        self.baseline_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        if hasattr(self, "order_export_btn"):
            self.order_export_btn.configure(state="disabled")
        self.status_var.set("Идёт проверка модов. Ход проверки виден в окне прогресса.")
        self._last_progress_line = ""
        self.progress_queue = queue.Queue()
        self._show_progress()
        worker = threading.Thread(
            target=self._scan_worker,
            args=(str(game_paths.game_root), selected_profile, force_rehash),
            daemon=True,
        )
        worker.start()
        self.after(100, self._poll_progress)

    def _scan_worker(self, game_root: str, profile_path: str | None, force_rehash: bool) -> None:
        try:
            report = scan_modules(
                game_root, bem_profile_path=profile_path, progress_callback=self.progress_queue.put,
                force_rehash=force_rehash, store=self.store,
            )
            baseline = compare_with_baseline(report)
            report["baseline_comparison"] = baseline
            report["experimental_model"] = predict_report(self.store, report, "Запуск игры")
            scan_id = self.store.record_scan(report)
            report_dir = Path(report["game_root_path"]) / "BCI_Reports"
            json_path, html_path = export_report(report, report_dir)
            self.progress_queue.put({
                "type": "done", "report": report, "report_dir": str(report_dir),
                "json_path": str(json_path), "html_path": str(html_path),
            })
        except Exception as exc:
            self.progress_queue.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"})

    def _poll_progress(self) -> None:
        finished = False
        while True:
            try:
                item = self.progress_queue.get_nowait()
            except queue.Empty:
                break
            if item.get("type") == "progress":
                percent = int(item.get("percent", 0))
                label = item.get("label", "Обработка")
                current = item.get("current", 0)
                total = item.get("total", 0)
                detail = item.get("detail", "")
                if self.progress_bar is not None:
                    self.progress_bar.configure(value=percent)
                if self.progress_label is not None:
                    suffix = f" ({current}/{total})" if total else ""
                    self.progress_label.configure(text=f"{percent}% · {label}{suffix}")
                if self.progress_detail is not None:
                    self.progress_detail.configure(text=detail)
                self._log_progress(f"{percent:3d}%  {label}" + (f" — {detail}" if detail else ""))
            elif item.get("type") == "done":
                self._scan_complete(
                    item["report"], Path(item["report_dir"]), Path(item["json_path"]), Path(item["html_path"])
                )
                finished = True
                break
            elif item.get("type") == "error":
                self._scan_failed(item.get("error", "Неизвестная ошибка"))
                finished = True
                break
        if self.scanning and not finished:
            self.after(100, self._poll_progress)

    def _close_progress(self) -> None:
        if self.progress_window is not None and self.progress_window.winfo_exists():
            self.progress_window.destroy()
        self.progress_window = None
        self.progress_bar = None
        self.progress_label = None
        self.progress_detail = None
        self.progress_log = None
        self.progress_log_toggle = None
        self._progress_log_visible = False

    def _scan_failed(self, error: str) -> None:
        self.scanning = False
        self.scan_btn.configure(state="normal")
        self.baseline_btn.configure(state="normal" if self.report else "disabled")
        self.export_btn.configure(
            state="normal" if self.latest_html is not None and self.latest_html.exists() else "disabled"
        )
        if hasattr(self, "order_export_btn"):
            self.order_export_btn.configure(state="normal" if self.report else "disabled")
        self.profile_status_var.set("Проверка профиля не завершена. Подробности ошибки показаны в отдельном окне.")
        self.status_var.set("Сканирование не завершено.")
        self._close_progress()
        messagebox.showerror("Ошибка BCI", error)

    def _scan_complete(self, report, report_dir: Path, json_path: Path, html_path: Path) -> None:
        self.report = report
        self.current_scan_id = report.get("scan_id")
        self.report_dir = report_dir
        self.latest_json = json_path
        self.latest_html = html_path
        self.scanning = False
        self.scan_btn.configure(state="normal")
        self.baseline_btn.configure(state="normal")
        self.export_btn.configure(state="normal")
        self.order_export_btn.configure(state="normal")
        summary = report["summary"]
        baseline = report.get("baseline_comparison", {})
        if baseline.get("available"):
            baseline_text = (
                f"Новых: {len(baseline['new_modules'])}, изменённых: {len(baseline['changed_modules'])}, "
                f"удалённых: {len(baseline['removed_modules'])}; кандидатов на проверку: {baseline.get('candidate_count', 0)}"
            )
        else:
            baseline_text = "Контрольная сборка ещё не сохранена"

        total_modules = summary.get("modules_found", 0)
        profile_scope_total = report.get("profile_scope_modules", summary.get("profile_scope_modules", total_modules))
        in_analysis = summary.get("modules_in_analysis", summary.get("modules_enabled", 0))
        known_count = report.get("enabled_known_modules", 0)
        unknown_count = report.get("enabled_unknown_modules", profile_scope_total - known_count)
        state = report.get("enabled_state_status", "unknown")
        source_path = report.get("launcher_order_source") or ""
        source_label = report.get("launcher_order_source_type") or ("Launcher" if source_path else "профиль")
        profile_info = report.get("profile_info", {}) or {}
        profile_detail_parts = []
        if profile_info.get("profile_name"):
            profile_detail_parts.append(f"профиль: {profile_info['profile_name']}")
        if profile_info.get("profile_id"):
            profile_detail_parts.append(f"ID: {profile_info['profile_id']}")
        if profile_info.get("game_mode") and profile_info.get("game_mode") != "unknown":
            profile_detail_parts.append("режим: " + ("Singleplayer" if profile_info["game_mode"] == "singleplayer" else "Multiplayer"))
        if profile_info.get("snapshot_created_at"):
            profile_detail_parts.append(f"mtime файла снимка UTC: {profile_info['snapshot_created_at']}")
        elif profile_info.get("load_order_created_at"):
            profile_detail_parts.append(f"mtime файла JSON UTC: {profile_info['load_order_created_at']}")
        profile_details_text = " · ".join(profile_detail_parts)
        if state == "known":
            state_text = f"Состояние всех модулей в профиле подтверждено ({known_count}/{profile_scope_total}); источник: {source_label}. Установлено всего: {total_modules}."
        elif state == "partial":
            state_text = (
                f"Профиль {source_label}: состояние подтверждено для {known_count}/{profile_scope_total} модулей; "
                f"ещё {unknown_count} с неизвестным статусом включены в анализ, чтобы не пропустить возможную проблему."
            )
        else:
            discovery_notes = report.get("launcher_discovery_notes", [])
            notes_text = " ".join(str(note).casefold() for note in discovery_notes)
            if "bem" in notes_text or "vortex" in notes_text:
                state_text = (
                    "Обнаружены сведения о Vortex/BEM, но активный профиль именно этой установки не удалось подтвердить. "
                    f"BCI консервативно проверил {in_analysis} из {total_modules} установленных модов; их Enabled-состояние неизвестно."
                )
            else:
                state_text = (
                    "Активный профиль Vortex/BEM/Launcher не найден. Состояние включения неизвестно; "
                    f"BCI проверил {in_analysis} из {total_modules} установленных модов, чтобы не пропустить возможную проблему."
                )
        if profile_details_text:
            state_text += " Параметры профиля: " + profile_details_text + "."
        elif report.get("launcher_discovery_notes"):
            state_text += " Диагностика: " + str(report["launcher_discovery_notes"][0])
        self.profile_status_var.set(state_text)
        self.summary_var.set(
            f"В анализ включено: {in_analysis} из {total_modules} модов · "
            f"Подтверждённо включено: {summary.get('modules_confirmed_enabled', 0)} · "
            f"Находок: {summary['findings_total']} · Критических: {summary['critical']} · "
            f"Высокий приоритет: {summary['high']} · Средний: {summary['medium']} · {baseline_text}"
        )
        order = report.get("load_order_analysis", {})
        if report.get("load_order_known"):
            order_text = f"Текущий порядок найден в {source_label} и используется только для аудита; собственную рекомендацию BCI рассчитывает независимо."
        elif source_path:
            order_text = "Файл профиля найден, но он не содержит подтверждённого состояния для всех модов. Реальные нарушения порядка не оцениваются."
        else:
            order_text = "Текущий порядок не найден. BCI строит собственную рекомендацию по зависимостям и файловым свидетельствам; активный набор модов всё ещё может быть неполным."
        recommendation_status = order.get("recommendation_status", "provisional")
        if recommendation_status == "ready":
            recommendation_text = "Рекомендация построена по подтверждённому профилю; BCI не применяет её автоматически."
        elif recommendation_status == "ready_with_warnings":
            warning_count = len(order.get("manual_use_warnings", [])) + sum(1 for issue in order.get("issues", []) if issue.get("severity") == "Medium")
            recommendation_text = f"Порядок рассчитан по подтверждённому профилю, но осталось предупреждений: {warning_count}. В том числе проверьте файловые пары и версии перед переносом."
        elif recommendation_status == "blocked_by_duplicate_ids":
            recommendation_text = "Рекомендация заблокирована: обнаружены повторяющиеся Module ID. Сначала устраните дубли."
        elif recommendation_status == "blocked_by_cycles":
            recommendation_text = "Рекомендация заблокирована: циклические правила нельзя выполнить одновременно."
        elif recommendation_status == "blocked_by_critical_issues":
            recommendation_text = "Рекомендация требует проверки: есть критические проблемы зависимостей или несовместимости."
        else:
            recommendation_text = "Порядок предварительный: активный профиль или Enabled-состояние подтверждены не полностью. Не переносите его автоматически."
        current_order_status = format_current_order_status(order)
        self.order_summary_var.set(
            f"{order_text} Ограничений: {len(order.get('constraints', []))} · "
            f"Нарушений подтверждённого порядка: {current_order_status} · Циклов: {len(order.get('cycles', []))}. "
            f"{recommendation_text}"
        )
        pair_evidence = order.get("pairwise_order_evidence", {})
        coverage = pair_evidence.get("coverage_pct")
        coverage_text = f"{coverage:.1f}%" if isinstance(coverage, (int, float)) else "не рассчитывается"
        self.order_evidence_var.set(
            f"Порядок определён правилами для {pair_evidence.get('determined_pairs', 0)} из "
            f"{pair_evidence.get('total_pairs', 0)} пар модов ({coverage_text}); "
            f"не определён для {pair_evidence.get('undetermined_pairs', 0)} пар. "
            "Это покрытие метаданными, а не шанс совместимости. В одной группе модули не имеют установленного взаимного приоритета."
        )
        model_result = report.get("experimental_model")
        if model_result:
            probability = model_result.get("regression_score", 0.0) * 100
            self.training_status_var.set(
                f"Теневая ML-оценка для сценария «{model_result.get('scenario')}»: {probability:.1f}% — "
                f"{model_result.get('class')}. Это оценка всей сборки, не диагноз и не поиск виновного мода."
            )
        else:
            self.training_status_var.set("Отчёт автоматически сохранён в локальную базу. ML-оценка появится после обучения модели.")
        self.status_var.set(f"Сканирование завершено. Отчёты сохранены в {report_dir}")
        self._fill_trees(report)
        self._refresh_training_view()
        if self.progress_bar is not None:
            self.progress_bar.configure(value=100)
        if self.progress_label is not None:
            self.progress_label.configure(text="100% · Анализ завершён")
        if self.progress_detail is not None:
            self.progress_detail.configure(text=f"JSON: {json_path}\nHTML: {html_path}")
        self._log_progress("100%  Анализ завершён. Отчёты сформированы.")
        self.after(900, self._close_progress)

    def _fill_trees(self, report) -> None:
        for tree in (self.ranking_tree, self.finding_tree, self.module_tree, self.order_tree, self.order_pairs_tree):
            for item in tree.get_children():
                tree.delete(item)
        for i, row in enumerate(report.get("ranking", []), 1):
            impact = row.get("candidate_impact")
            self.ranking_tree.insert("", "end", values=(
                i, row.get("baseline_status", ""), row["name"], row["module_id"], row["risk_index"],
                impact if impact is not None else "—", row["finding_count"], ", ".join(row["related_mods"]),
            ))
        self.findings_by_id = {f["finding_id"]: f for f in report.get("findings", [])}
        for finding in report.get("findings", []):
            self.finding_tree.insert("", "end", iid=finding["finding_id"], values=(
                finding["severity"], finding["category"], finding["title"], ", ".join(finding["modules"]),
                finding["risk_points"], finding["confidence"],
            ))
        for module in report.get("modules", []):
            if module.get("excluded_by_game_mode") == "singleplayer":
                enabled = "Вне режима SP"
            elif not module.get("enabled_known"):
                enabled = "Неизвестно"
            else:
                enabled = "Да" if module["enabled"] else "Нет"
            self.module_tree.insert("", "end", values=(
                enabled, module["name"], module["module_id"], module["version"],
                "Да" if module["is_official"] else "Нет",
                module["load_order"] + 1 if module["load_order"] is not None else "—",
                module["folder"],
            ))
        for item in report.get("load_order_analysis", {}).get("recommended_order", []):
            current = item.get("current_position")
            reasons = "; ".join(item.get("reasons", [])) or item.get("placement_basis", "Взаимный порядок не задан метаданными")
            if item.get("reasons"):
                reasons = item.get("placement_basis", "") + "; " + reasons
            known_pct = item.get("relative_order_coverage_pct")
            known_text = (
                f"{known_pct:.0f}% ({item.get('relative_order_determined_pairs', 0)}/"
                f"{item.get('relative_order_total_pairs', 0)})" if isinstance(known_pct, (int, float)) else "—"
            )
            self.order_tree.insert("", "end", values=(
                item["position"], item.get("order_group") if item.get("order_group") is not None else "Не разрешена",
                current if current is not None else "Неизвестно", item["name"], item["module_id"], known_text, reasons,
            ))

        for item in report.get("load_order_analysis", {}).get("file_interactions", []):
            if not item.get("order_sensitive"):
                continue
            direction = {
                "determined": "Задано правилами",
                "undetermined": "Не определено",
                "conflicting": "Противоречие",
            }.get(item.get("direction_status"), "Неизвестно")
            self.order_pairs_tree.insert("", "end", values=(
                " ↔ ".join(item.get("modules", [])),
                item.get("xml_conflict_count", 0),
                len(item.get("resource_different_paths", [])),
                item.get("evidence_confidence", "не оценено"),
                direction,
                item.get("recommendation", ""),
            ))

    def _show_finding(self, _event=None) -> None:
        selected = self.finding_tree.selection()
        if not selected:
            return
        finding = self.findings_by_id.get(selected[0])
        if not finding:
            return
        win = tk.Toplevel(self)
        win.title(finding["title"])
        win.geometry("900x650")
        win.configure(bg="#10151d")
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=finding["title"], style="Header.TLabel", wraplength=850).pack(anchor="w")
        ttk.Label(
            frame,
            text=f"Категория: {finding['category']} · Приоритет: {finding['severity']} · Баллы риска: {finding['risk_points']} · Уверенность: {finding['confidence']}",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=6)
        ttk.Label(frame, text="Описание", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 2))
        ttk.Label(frame, text=finding["description"], wraplength=850).pack(anchor="w")
        ttk.Label(frame, text="Рекомендация", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 2))
        ttk.Label(frame, text=finding.get("recommendation", ""), wraplength=850).pack(anchor="w")
        ttk.Label(frame, text="Технические доказательства", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 2))
        text = tk.Text(frame, wrap="none", bg="#19222e", fg="#e8edf4", insertbackground="white")
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(finding.get("evidence", {}), ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def _save_baseline(self) -> None:
        if not self.report:
            return
        if not messagebox.askyesno(
            "Сохранить контрольную сборку",
            "Сохранить текущую конфигурацию как контрольную сборку, которую ты уже проверил в игре?\n\n"
            "Сохраняй контрольную сборку только после успешных тестов в игре. Если BCI не смог определить состояние включения или порядок, "
            "снимок всё равно сохранит инвентаризацию, но не будет подтверждать активность модов.",
        ):
            return
        try:
            path = save_baseline(self.report)
            self.status_var.set(f"Контрольная сборка сохранена: {path}")
            messagebox.showinfo(
                "BCI",
                f"Контрольная сборка сохранена:\n{path}\n\nПосле добавления или обновления модов запусти новую проверку.",
            )
        except Exception as exc:
            messagebox.showerror("BCI", str(exc))

    def _export_load_order(self) -> None:
        if not self.report:
            return
        order = self.report.get("load_order_analysis", {})
        if not order.get("safe_for_manual_use") or order.get("manual_use_warnings"):
            status = order.get("recommendation_status", "provisional")
            reasons = order.get("blocking_reasons", []) + order.get("manual_use_warnings", [])
            if not messagebox.askyesno(
                "Порядок требует внимания",
                "BCI обнаружила неполноту активного профиля, блокирующие проблемы или файловые пары, "
                "для которых направление переопределения не установлено. В экспорт будут включены явные предупреждения; "
                "не переносите список как окончательный, пока не разберётесь с ними.\n\n"
                f"Статус: {status}\n"
                f"Причины: {'; '.join(reasons) or 'Активный набор модов не подтверждён.'}\n\n"
                "Всё равно сохранить текстовый список для изучения?",
            ):
                return
        default_name = "BCI_LoadOrder_Recommendation.txt"
        path = filedialog.asksaveasfilename(
            title="Экспортировать собственную рекомендацию BCI",
            initialfile=default_name,
            defaultextension=".txt",
            filetypes=[("Текстовый список BCI", "*.txt"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            saved = export_load_order(self.report, path)
            self.status_var.set(f"Рекомендованный порядок BCI экспортирован: {saved}")
            messagebox.showinfo(
                "Экспорт завершён",
                f"Файл сохранён:\n{saved}\n\nЭто текстовая инструкция для ручной перестановки. "
                "Файл не является прямым импортом Vortex и не изменяет профиль.",
            )
        except OSError as exc:
            messagebox.showerror("Не удалось экспортировать порядок", str(exc))

    def _open_report(self) -> None:
        if self.latest_html and self.latest_html.exists():
            webbrowser.open(self.latest_html.as_uri())

    def _refresh_training_view(self) -> None:
        try:
            stats = self.store.get_stats()
            self.training_stats_var.set(
                f"Всего отчётов: {stats['scans']} · Размечено результатами тестов: {stats['labelled']} · "
                f"Без регрессии: {stats['stable']} · С регрессией: {stats['regression']} · "
                f"Разных сборок: {stats['builds']}"
            )
            active = self.store.active_model()
            if active:
                manifest = active.get("manifest", {})
                holdout = manifest.get("holdout", {})
                self.model_status_var.set(
                    f"Активная модель: {active['model_id']} · примеров: {manifest.get('training_examples', 0)} · "
                    f"контрольная accuracy: {holdout.get('accuracy', 0):.3f} · режим: SHADOW (не влияет на рейтинг модов)."
                )
            else:
                self.model_status_var.set(
                    "Обученной ML-модели пока нет. Встроенный анализатор работает на экспертных правилах. Для обучения нужны подтверждённые результаты A/B/A: не менее 16 примеров, "
                    "по 5 каждого класса и 6 разных сборок. До этого работает статический анализатор."
                )
            for item in self.scan_history_tree.get_children():
                self.scan_history_tree.delete(item)
            outcomes = {
                "unknown": "Не проверено",
                "stable": "Тест пройден без регрессии",
                "regression": "Подтверждена регрессия",
            }
            for row in self.store.list_scans(100):
                date_text = str(row["created_at"]).replace("T", " ")[:19]
                self.scan_history_tree.insert("", "end", iid=row["scan_id"], values=(
                    date_text, row["modules_count"], row["findings_count"],
                    row["scenario"] or "—", outcomes.get(row["outcome"], row["outcome"]),
                ))
            self.train_btn.configure(state="normal" if stats["labelled"] >= 16 and stats["builds"] >= 6 else "disabled")
        except Exception as exc:
            self.training_status_var.set(f"Ошибка доступа к локальной базе: {type(exc).__name__}: {exc}")

    def _label_selected_scan(self) -> None:
        selected = self.scan_history_tree.selection()
        scan_id = selected[0] if selected else self.current_scan_id
        if not scan_id:
            messagebox.showinfo("BCI", "Сначала выполни полное сканирование. Затем выбери отчёт в списке.")
            return
        win = tk.Toplevel(self)
        win.title("BCI — результат игрового теста")
        win.geometry("570x410")
        win.resizable(False, False)
        win.transient(self)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Как закончился тест этой сборки?", style="Header.TLabel", wraplength=520).pack(anchor="w")
        ttk.Label(
            frame,
            text="Ставь метку только после реального игрового теста. Для подтверждения регрессии желательно выполнить A/B/A: базовая сборка → изменённая → базовая снова.",
            style="Sub.TLabel", wraplength=520,
        ).pack(anchor="w", pady=(6, 12))
        outcome_var = tk.StringVar(value="stable")
        ttk.Radiobutton(frame, text="Тест пройден без регрессии", variable=outcome_var, value="stable").pack(anchor="w", pady=3)
        ttk.Radiobutton(frame, text="Воспроизводимый вылет или функциональная регрессия", variable=outcome_var, value="regression").pack(anchor="w", pady=3)
        ttk.Label(frame, text="Проверенный сценарий:").pack(anchor="w", pady=(12, 4))
        scenario_var = tk.StringVar(value=SCENARIOS[0])
        scenario_box = ttk.Combobox(frame, textvariable=scenario_var, values=SCENARIOS, state="readonly")
        scenario_box.pack(fill="x")
        ttk.Label(frame, text="Что именно проверялось / что произошло:").pack(anchor="w", pady=(12, 4))
        notes = tk.Text(frame, height=4, wrap="word")
        notes.pack(fill="x")

        def save_label() -> None:
            try:
                self.store.label_scan(scan_id, outcome_var.get(), scenario_var.get(), notes.get("1.0", "end").strip())
            except Exception as exc:
                messagebox.showerror("BCI", str(exc), parent=win)
                return
            win.destroy()
            self.training_status_var.set("Результат теста сохранён локально и добавлен в набор данных.")
            self._refresh_training_view()

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Сохранить результат", command=save_label).pack(side="left")
        ttk.Button(actions, text="Отмена", command=win.destroy).pack(side="right")

    def _train_model(self) -> None:
        if self.training_in_progress:
            return
        self.training_in_progress = True
        self.train_btn.configure(state="disabled")
        self.training_status_var.set("Обучение выполняется локально. Данные не отправляются в сеть…")
        self.training_queue = queue.Queue()

        def worker() -> None:
            try:
                result = train_model(self.store)
                self.training_queue.put({"ok": True, "result": result})
            except Exception as exc:
                self.training_queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        threading.Thread(target=worker, daemon=True).start()
        self.after(150, self._poll_training)

    def _poll_training(self) -> None:
        try:
            result = self.training_queue.get_nowait()
        except queue.Empty:
            if self.training_in_progress:
                self.after(150, self._poll_training)
            return
        self.training_in_progress = False
        if result.get("ok"):
            manifest = result["result"]
            holdout = manifest.get("holdout", {})
            self.training_status_var.set(
                f"Модель {manifest['model_id']} обучена и сохранена. Контрольная accuracy: {holdout.get('accuracy', 0):.3f}; "
                f"precision: {holdout.get('precision_regression', 0):.3f}; recall: {holdout.get('recall_regression', 0):.3f}. "
                "Модель включена только в теневом режиме."
            )
            messagebox.showinfo("BCI — обучение завершено", "Новая локальная модель обучена.\n\nОна будет давать теневые оценки всей сборки, но не менять Risk Index и не объявлять виновные моды.")
        else:
            self.training_status_var.set(result.get("error", "Обучение не удалось."))
            messagebox.showwarning("BCI — обучение не выполнено", result.get("error", "Неизвестная ошибка"))
        self._refresh_training_view()

    def _export_model(self) -> None:
        from tkinter import filedialog
        destination = filedialog.asksaveasfilename(
            title="Экспортировать локальную ML-модель",
            defaultextension=".bci-model.zip",
            filetypes=[("BCI model package", "*.bci-model.zip"), ("ZIP архив", "*.zip")],
            initialfile="BCI_Model_Export.bci-model.zip",
        )
        if not destination:
            return
        try:
            path = export_active_model_package(self.store, destination)
        except Exception as exc:
            messagebox.showerror("BCI — экспорт модели", str(exc))
            return
        self.training_status_var.set(f"Пакет модели экспортирован: {path}")
        messagebox.showinfo("BCI", "Пакет модели создан. Его можно перенести на другой компьютер с совместимой версией BCI и импортировать без переустановки программы.")

    def _import_model(self) -> None:
        from tkinter import filedialog
        source = filedialog.askopenfilename(
            title="Выбери пакет модели BCI",
            filetypes=[("BCI model package", "*.bci-model.zip"), ("ZIP архив", "*.zip")],
        )
        if not source:
            return
        try:
            manifest = import_model_package(self.store, source)
        except Exception as exc:
            messagebox.showerror("BCI — импорт модели", str(exc))
            return
        self.training_status_var.set(f"Модель {manifest.get('model_id')} установлена и активирована без переустановки BCI.")
        self._refresh_training_view()
        messagebox.showinfo("BCI", "Модель установлена. При следующем сканировании она будет использоваться в теневом режиме.")

    def _open_data_folder(self) -> None:
        try:
            path = self.store.root
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(path))
            else:
                webbrowser.open(path.as_uri())
        except Exception as exc:
            messagebox.showerror("BCI", f"Не удалось открыть папку данных: {exc}")

    def _help(self) -> None:
        text = (
            "КАК ПРОВЕРИТЬ СБОРКУ\n\n"
            "1. Выбери папку Mount & Blade II: Bannerlord — внутри неё должны быть Modules и bin.\n"
            "2. Нажми «Проверить моды». BCI просканирует установленные модули и попробует найти точный профиль BEM/Launcher.\n"
            "3. Сначала посмотри вкладку «Приоритет проверки», затем открой находки двойным щелчком, чтобы прочитать объяснение и доказательства.\n"
            "4. Во вкладке «Порядок загрузки» BCI самостоятельно строит порядок по графу зависимостей и файловым взаимодействиям. Группы показывают, где взаимный порядок не задан. Текстовый список можно экспортировать для ручной расстановки в Vortex; BCI не изменяет Vortex автоматически.\n\n"
            "СТАТУС ПРОФИЛЯ\n\n"
            "Если статус модов подтверждён не полностью или профиль не найден, BCI включает неизвестные по состоянию моды в анализ. "
            "Это помогает не пропустить возможную проблему, но может добавить предупреждения от модов, которые фактически выключены. "
            "Проверь профиль BEM/Launcher и повтори сканирование.\n\n"
            "КОНТРОЛЬНАЯ СБОРКА\n\n"
            "Сохраняй её только после того, как проверил эту конфигурацию в игре. Она поможет заметить новые, изменённые и удалённые моды.\n\n"
            "ВАЖНО\n\n"
            "Совпадение файлов или XML не доказывает конфликт или вылет. Баллы риска — это приоритет проверки, а не вероятность краша. "
            "Экспериментальная ML-модель не определяет виновный мод. BCI не меняет файлы модов и порядок загрузки."
        )
        messagebox.showinfo("BCI — краткая справка", text)



if __name__ == "__main__":
    BCIApp().mainloop()
