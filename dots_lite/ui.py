# Extracted from remote_transport/mac_ui.py (Dots2Codex, MIT).
"""Small native Standard Additions UI with TTY fallback, no simulated clicks."""
from __future__ import annotations
import os
import shutil
import subprocess
import sys


class Cancelled(Exception):
    pass


class UI:
    def __init__(self, *, run=subprocess.run, input_fn=input, tty=None, native=None):
        self.run, self.input = run, input_fn
        self.tty = sys.stdin.isatty() if tty is None else tty
        self.native = (sys.platform == 'darwin' and shutil.which('osascript') is not None) if native is None else native

    def _native(self, script, args):
        if not self.native: return None
        try:
            result = self.run(['/usr/bin/osascript', '-e', 'on run argv\n' + script + '\nend run', '--', *args],
                              capture_output=True, text=True, timeout=180, check=False)
        except subprocess.TimeoutExpired: raise Cancelled() from None
        except OSError:
            if self.tty: return None
            raise Cancelled() from None
        if result.returncode:
            if '-128' in result.stderr or '-1712' in result.stderr: raise Cancelled()
            if self.tty: return None
            raise Cancelled()
        value = result.stdout.rstrip('\n')
        if value in ('false', '__TIMEOUT__'): raise Cancelled()
        return value

    def _read(self, prompt):
        if not self.tty: raise Cancelled()
        try: return self.input(prompt)
        except (EOFError, KeyboardInterrupt): raise Cancelled() from None

    def confirm(self, message):
        result = self._native('set r to display dialog (item 1 of argv) with title "Dots2Codex Lightweight v3" buttons {"Cancel", "Continue"} default button "Cancel" cancel button "Cancel" giving up after 120\nif gave up of r then return "__TIMEOUT__"\nreturn button returned of r', [message])
        if result is not None: return result == 'Continue'
        return self._read(message + '\nType yes to continue [no]: ').strip().lower() == 'yes'

    def choose(self, message, choices, default=None):
        choices = list(choices)
        if not choices: raise ValueError('launcher_empty_choices')
        if default is not None and default not in choices: raise ValueError('launcher_invalid_default_choice')
        # Data goes in argv; quotes/newlines never become AppleScript source.
        result = self._native('set itemsList to items 3 thru -1 of argv\nset defaultItems to {}\nif (item 2 of argv) is not "" then set defaultItems to {item 2 of argv}\nset r to choose from list itemsList with prompt (item 1 of argv) with title "Dots2Codex Lightweight v3" default items defaultItems without multiple selections allowed and empty selection allowed\nif r is false then return "false"\nreturn item 1 of r', [message, default or '', *choices])
        if result is None:
            print(message, flush=True)
            for n, item in enumerate(choices, 1):
                print(f'  {n}. {item}' + (' (default)' if item == default else ''), flush=True)
            prompt = f'Choose a number (blank selects {default}; q cancels): ' if default is not None else 'Choose a number (blank cancels): '
            value = self._read(prompt).strip()
            if not value and default is not None: result = default
            else:
                if not value.isdigit() or not 1 <= int(value) <= len(choices): raise Cancelled()
                result = choices[int(value)-1]
        if result not in choices: raise Cancelled()
        return result

    def text(self, message, default=''):
        result = self._native('set r to display dialog (item 1 of argv) default answer (item 2 of argv) with title "Dots2Codex Lightweight v3" buttons {"Cancel", "Continue"} default button "Continue" cancel button "Cancel" giving up after 120\nif gave up of r then return "__TIMEOUT__"\nreturn text returned of r', [message, str(default)])
        if result is None:
            result = self._read(f'{message} [{default}]: ').strip() or default
        if not result: raise Cancelled()
        return result

    def file(self, message, default=''):
        result = self._native('return POSIX path of (choose file with prompt (item 1 of argv))', [message])
        return self.text(message, default) if result is None else result

    def folder(self, message, default=''):
        result = self._native('return POSIX path of (choose folder with prompt (item 1 of argv))', [message])
        return self.text(message, default) if result is None else result

    def notify(self, message):
        # Status text is safe; never pass join codes or credential contents here.
        result = self._native('display dialog (item 1 of argv) with title "Dots2Codex Lightweight v3" buttons {"OK"} default button "OK" giving up after 120\nreturn "ok"', [message])
        if result is None: print(message, flush=True)
