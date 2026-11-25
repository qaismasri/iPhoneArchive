"""Windows desktop interface and command line for the iPhone archive."""

import argparse
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import winreg

import archive
from wpd import WpdError, attached_phones


SETTINGS = Path(os.environ['LOCALAPPDATA']) / 'iPhoneArchive' / 'settings.json'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'iPhoneArchive'


def startup_command():
    python = Path(sys.executable).with_name('pythonw.exe')
    return subprocess.list2cmdline([str(python if python.is_file() else sys.executable),
                                    '-B', str(Path(__file__).resolve())])


def starts_with_windows():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0] == startup_command()
    except OSError:
        return False


def set_startup(enabled):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            command = startup_command()
            if len(command) >= 260:
                raise OSError('The project path is too long for Windows startup. Open the launcher manually.')
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def load_settings():
    try:
        stored = json.loads(SETTINGS.read_text(encoding='utf-8'))
        if not isinstance(stored.get('folder'), str) or not isinstance(stored.get('auto'), bool):
            raise ValueError('Settings format is invalid')
        return stored, None
    except FileNotFoundError:
        return {'folder': str(archive.default_archive()), 'auto': False}, None
    except (ValueError, OSError, AttributeError) as error:
        return {'folder': str(archive.default_archive()), 'auto': False}, str(error)


def describe(count, size):
    return f'{count:,} files · {size / 1_000_000_000:.2f} GB'


def create_root():
    try:
        return tk.Tk()
    except tk.TclError:
        # Some Windows sessions cannot normalize the installed Tcl paths.
        # The extended-length form gives Tcl the actual installation path.
        library = Path(sys.base_prefix) / 'tcl'
        tcl = library / f'tcl{tk.TclVersion}'
        toolkit = library / f'tk{tk.TkVersion}'
        if not (tcl / 'init.tcl').is_file() or not (toolkit / 'tk.tcl').is_file():
            raise
        os.environ['TCL_LIBRARY'] = '\\\\?\\' + str(tcl)
        os.environ['TK_LIBRARY'] = '\\\\?\\' + str(toolkit)
        return tk.Tk()


class Window:
    def __init__(self, root):
        self.root = root
        self.events = Queue()
        self.action = None
        self.stop = Event()
        self.polling = False
        self.phones = ()
        self.pending_auto = True
        self.closing = False
        self.auto_warned = False
        settings, settings_error = load_settings()
        self.folder = tk.StringVar(value=settings['folder'])
        self.auto = tk.BooleanVar(value=settings['auto'])
        self.start_on_login = tk.BooleanVar(value=starts_with_windows())
        self.connection = tk.StringVar(value='Checking USB…')
        self.status = tk.StringVar(value='Ready')
        self.summary = tk.StringVar(value='')
        self.setup_widgets()
        if settings_error:
            self.note(f'Could not load preferences: {settings_error}')
        self.root.after(100, self.handle_events)
        self.root.after(100, self.poll)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def setup_widgets(self):
        self.root.title('iPhone Archive')
        self.root.geometry('790x580')
        self.root.minsize(650, 480)
        self.root.configure(background='#f4f7fb')
        style = ttk.Style(self.root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('TFrame', background='#f4f7fb')
        style.configure('TLabel', background='#f4f7fb', foreground='#19304f', font=('Segoe UI', 10))
        style.configure('TButton', font=('Segoe UI', 10), padding=(12, 6))
        style.configure('TCheckbutton', background='#f4f7fb', font=('Segoe UI', 10))
        style.configure('Accent.TButton', background='#235ac1', foreground='white', padding=(12, 6))
        style.map('Accent.TButton', background=[('disabled', '#8299bc'), ('active', '#194ba7')])
        style.configure('Horizontal.TProgressbar', background='#235ac1', troughcolor='#dce6f6',
                        bordercolor='#dce6f6', lightcolor='#235ac1', darkcolor='#235ac1')

        header = tk.Frame(self.root, bg='#142947', padx=22, pady=18)
        header.pack(fill='x')
        tk.Label(header, text='iPhone Archive', bg='#142947', fg='white',
                 font=('Segoe UI', 21, 'bold')).pack(side='left')
        tk.Label(header, textvariable=self.connection, bg='#142947', fg='#d2e3ff',
                 font=('Segoe UI', 11)).pack(side='right')

        body = ttk.Frame(self.root, padding=18)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(7, weight=1)
        ttk.Label(body, text='Archive folder').grid(row=0, column=0, sticky='w')
        pathbar = ttk.Frame(body)
        pathbar.grid(row=1, column=0, sticky='ew', pady=(4, 10))
        ttk.Entry(pathbar, textvariable=self.folder, state='readonly').pack(side='left', fill='x', expand=True)
        self.choose = ttk.Button(pathbar, text='Choose…', command=self.choose_folder)
        self.choose.pack(side='left', padx=(7, 0))
        ttk.Button(pathbar, text='Open folder', command=self.open_folder).pack(side='left', padx=(7, 0))

        actions = ttk.Frame(body)
        actions.grid(row=2, column=0, sticky='ew')
        self.import_button = ttk.Button(actions, text='Import now', style='Accent.TButton',
                                        command=lambda: self.launch('import'))
        self.scan_button = ttk.Button(actions, text='Check iPhone', command=lambda: self.launch('scan'))
        self.verify_button = ttk.Button(actions, text='Verify archive', command=lambda: self.launch('verify'))
        self.pause_button = ttk.Button(actions, text='Pause', command=self.pause, state='disabled')
        for button in (self.import_button, self.scan_button, self.verify_button, self.pause_button):
            button.pack(side='left', padx=(0, 7))
        prefs = ttk.Frame(body)
        prefs.grid(row=3, column=0, sticky='ew', pady=(8, 12))
        ttk.Checkbutton(prefs, text='Import when iPhone connects', variable=self.auto,
                        command=self.toggle_auto).pack(side='left', padx=(0, 14))
        ttk.Checkbutton(prefs, text='Start with Windows', variable=self.start_on_login,
                        command=self.toggle_startup).pack(side='left')

        status_bar = ttk.Frame(body)
        status_bar.grid(row=4, column=0, sticky='ew', pady=(4, 4))
        ttk.Label(status_bar, textvariable=self.status).pack(side='left')
        ttk.Label(status_bar, textvariable=self.summary).pack(side='right')
        self.batch_bar = ttk.Progressbar(body, maximum=1)
        self.batch_bar.grid(row=5, column=0, sticky='ew')
        self.file_bar = ttk.Progressbar(body, maximum=1)
        self.file_bar.grid(row=6, column=0, sticky='ew', pady=(5, 12))

        history = ttk.Frame(body)
        history.grid(row=7, column=0, sticky='nsew')
        self.log = tk.Text(history, wrap='word', height=9, padx=10, pady=8, bd=0,
                           bg='white', fg='#19304f', font=('Segoe UI', 10), state='disabled')
        self.log.pack(side='left', expand=True, fill='both')
        scroll = ttk.Scrollbar(history, command=self.log.yview)
        scroll.pack(side='right', fill='y')
        self.log.configure(yscrollcommand=scroll.set)

        menu = tk.Menu(self.root)
        archive_menu = tk.Menu(menu, tearoff=False)
        archive_menu.add_command(label='Open import log', command=self.open_log)
        archive_menu.add_command(label='Move earlier archive…', command=self.migrate)
        menu.add_cascade(label='Archive', menu=archive_menu)
        self.root.config(menu=menu)

    def note(self, message):
        self.log.config(state='normal')
        self.log.insert('end', message + '\n')
        if int(self.log.index('end-1c').split('.')[0]) > 500:
            self.log.delete('1.0', '51.0')
        self.log.see('end')
        self.log.config(state='disabled')

    def save_settings(self):
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            temp = SETTINGS.with_suffix('.tmp')
            temp.write_text(json.dumps({'folder': self.folder.get(), 'auto': self.auto.get()}, indent=2),
                            encoding='utf-8')
            temp.replace(SETTINGS)
        except OSError as error:
            messagebox.showwarning('Preferences could not be saved', str(error), parent=self.root)

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title='Choose the archive folder',
                                         initialdir=Path(self.folder.get()).parent)
        if folder:
            try:
                self.folder.set(str(archive.validate_archive(Path(folder))))
            except OSError as error:
                messagebox.showerror('Folder could not be selected', str(error), parent=self.root)
                return
            self.save_settings()
            self.pending_auto = True

    def open_folder(self):
        path = Path(self.folder.get())
        try:
            os.startfile(path)
        except OSError as error:
            messagebox.showerror('Folder could not be opened', str(error), parent=self.root)

    def open_log(self):
        path = Path(self.folder.get()) / '_archive' / 'import.log'
        try:
            os.startfile(path)
        except OSError as error:
            messagebox.showerror('Import log could not be opened', str(error), parent=self.root)

    def toggle_auto(self):
        self.pending_auto = self.auto.get()
        self.auto_warned = False
        self.save_settings()
        self.maybe_import()

    def toggle_startup(self):
        try:
            set_startup(self.start_on_login.get())
        except OSError as error:
            self.start_on_login.set(not self.start_on_login.get())
            messagebox.showerror('Windows startup could not be updated', str(error), parent=self.root)

    def poll(self):
        if not self.action and not self.polling:
            self.polling = True

            def read_connection():
                try:
                    self.events.put(('connection', attached_phones()))
                except OSError as error:
                    self.events.put(('connection-error', str(error)))

            Thread(target=read_connection, daemon=True).start()
        self.root.after(3500, self.poll)

    def maybe_import(self):
        if self.auto.get() and self.pending_auto and len(self.phones) == 1 and not self.action:
            self.pending_auto = False
            self.launch('auto')

    def emit(self, event, value):
        self.events.put((event, value))

    def launch(self, action, destination=None):
        if self.action:
            return
        self.action = action
        self.stop.clear()
        if action == 'import':
            self.pending_auto = False
        self.status.set('Starting…')
        self.summary.set('')
        self.batch_bar.configure(value=0, maximum=1)
        self.file_bar.configure(value=0, maximum=1)
        self.set_busy(True)
        folder = Path(self.folder.get())

        def work():
            try:
                if action == 'scan':
                    result = archive.preview(folder, self.emit, self.stop)
                elif action == 'verify':
                    result = archive.verify(folder, self.emit, self.stop)
                elif action == 'move':
                    result = (destination, *archive.move_archive(archive.PREVIOUS, destination, self.emit))
                else:
                    result = archive.import_phone(folder, self.emit, self.stop)
                self.emit('done', (action, result))
            except archive.Paused as error:
                self.emit('paused', str(error))
            except Exception as error:
                self.emit('error', (action, error))

        Thread(target=work, daemon=True).start()

    def set_busy(self, busy):
        for button in (self.import_button, self.scan_button, self.verify_button, self.choose):
            button.config(state='disabled' if busy else 'normal')
        self.pause_button.config(state='normal' if busy and self.action != 'move' else 'disabled')

    def pause(self):
        self.status.set('Pausing…')
        self.stop.set()
        self.pending_auto = False

    def close(self):
        if self.action == 'move':
            messagebox.showinfo('Archive move in progress', 'Keep this window open until verification finishes.',
                                parent=self.root)
        elif self.action:
            if messagebox.askyesno('Pause and close?', 'Pause this operation and close the archive app?',
                                   parent=self.root):
                self.closing = True
                self.pause()
        else:
            self.root.destroy()

    def migrate(self):
        if self.action:
            return
        if not archive.PREVIOUS.is_dir():
            messagebox.showinfo('Earlier archive', 'The earlier archive folder could not be found.', parent=self.root)
            return
        parent = filedialog.askdirectory(parent=self.root, title='Choose where to move the PhotoArchive folder',
                                        initialdir=archive.REQUESTED.parent)
        if not parent:
            return
        destination = Path(parent) / archive.PREVIOUS.name
        if messagebox.askyesno('Move earlier archive?',
                               f'Move the existing files and their history to\n{destination}\n\n'
                               'The archive will be verified before and after the move.', parent=self.root):
            self.launch('move', destination)

    def handle_events(self):
        for _ in range(100):
            try:
                name, value = self.events.get_nowait()
            except Empty:
                break
            if name == 'status':
                self.status.set(value)
            elif name == 'log':
                self.note(value)
            elif name == 'files':
                complete, total, _ = value
                self.batch_bar.configure(maximum=max(1, total), value=complete)
                self.summary.set(f'{complete:,} / {total:,} files')
            elif name == 'bytes':
                done, total = value
                self.file_bar.configure(maximum=total or max(1, done * 2), value=done)
            elif name == 'connection':
                self.polling = False
                if self.phones != value:
                    self.phones = value
                    self.pending_auto = True
                    self.auto_warned = False
                labels = {0: 'Connect your iPhone', 1: 'iPhone connected'}
                self.connection.set(labels.get(len(value), 'Connect one iPhone'))
                self.maybe_import()
            elif name == 'connection-error':
                self.polling = False
                if self.connection.get() != 'USB unavailable':
                    self.note(value)
                self.connection.set('USB unavailable')
            else:
                self.action = None
                self.set_busy(False)
                if self.closing:
                    self.root.destroy()
                    return
                if name == 'done':
                    self.finished(*value)
                elif name == 'paused':
                    self.status.set('Paused')
                    self.note(value)
                elif name == 'error':
                    self.failed(*value)
        self.root.after(100, self.handle_events)

    def finished(self, action, result):
        if action == 'scan':
            known, pending = result
            message = f'{known:,} matched the archive history. {len(pending):,} need USB reads.'
            if pending:
                self.note(f'First in date order: {pending[0].name}')
        elif action == 'verify':
            message = f'SHA-256 verified · {describe(*result)}'
        elif action == 'move':
            destination, count, size = result
            self.folder.set(str(destination))
            self.save_settings()
            message = f'Moved and verified · {describe(count, size)}'
        else:
            message = (f'{result["saved"]:,} saved, {result["skipped"]:,} already archived, '
                       f'{result["duplicates"]:,} content duplicates')
        self.status.set('Complete')
        self.note(message)
        if action != 'scan':
            messagebox.showinfo('Archive complete', message, parent=self.root)

    def failed(self, action, error):
        self.status.set('Needs attention')
        self.note(str(error))
        title = 'File verification failed' if isinstance(error, archive.IntegrityError) else 'Archive operation stopped'
        if action == 'auto' and isinstance(error, WpdError) and error.hresult is None:
            self.status.set('Unlock the iPhone and approve Trust')
            if not self.auto_warned:
                self.auto_warned = True
                messagebox.showwarning(title, str(error), parent=self.root)
            self.root.after(8500, self.retry_auto)
        else:
            messagebox.showerror(title, str(error), parent=self.root)

    def retry_auto(self):
        if self.auto.get() and not self.action:
            self.pending_auto = True
            self.maybe_import()


def terminal(event, value):
    if event in ('status', 'log'):
        print(value, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('gui', 'scan', 'import', 'verify', 'move-previous'), nargs='?', default='gui')
    parser.add_argument('--archive', type=Path, default=archive.default_archive())
    parser.add_argument('--destination', type=Path, default=archive.REQUESTED)
    args = parser.parse_args()
    if args.action == 'gui':
        root = create_root()
        Window(root)
        root.mainloop()
        return 0
    try:
        if args.action == 'scan':
            known, pending = archive.preview(args.archive, terminal)
            print(f'{known:,} previously archived items matched, {len(pending):,} need USB reads.')
            if pending:
                print(f'First candidate: {pending[0].name}, iPhone date {archive.source_date(pending[0])}')
        elif args.action == 'import':
            print(archive.import_phone(args.archive, terminal))
        elif args.action == 'verify':
            print(f'Verified {describe(*archive.verify(args.archive, terminal))}')
        else:
            result = archive.move_archive(archive.PREVIOUS, args.destination, terminal)
            print(f'Moved and verified {describe(*result)} in {args.destination}.')
        return 0
    except (archive.Paused, KeyboardInterrupt):
        print('Stopped. Previously verified files are saved.', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
