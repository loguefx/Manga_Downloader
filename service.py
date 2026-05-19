"""
Windows Service wrapper for Manga Downloader.

When installed, Windows starts MangaDownloader.exe automatically on boot
and keeps it running in the background — no console window, no manual start.

Usage (run Command Prompt / PowerShell as Administrator):

  MangaDownloader.exe install   -- install & register the service
  MangaDownloader.exe start     -- start the service
  MangaDownloader.exe stop      -- stop the service
  MangaDownloader.exe restart   -- restart the service
  MangaDownloader.exe remove    -- stop & uninstall the service

After installing, the service also starts automatically on every reboot.
The web dashboard is available at http://localhost:8080 once the service is running.
"""

import logging
import sys
import threading

import win32event
import win32service
import win32serviceutil
import servicemanager

import paths


class MangaDownloaderService(win32serviceutil.ServiceFramework):
    _svc_name_         = "MangaDownloader"
    _svc_display_name_ = "Manga Downloader"
    _svc_description_  = (
        "Downloads manga from MangaDex and third-party sites on a schedule. "
        "Serves the web dashboard at http://localhost:8080. "
        "Starts automatically with Windows."
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._running    = True

        logging.basicConfig(
            filename=str(paths.LOG_FILE),
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)
        self._running = False

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        logging.info("Manga Downloader service starting...")
        self._run()

    def _run(self):
        import traceback
        try:
            import app as flask_app

            flask_app._setup_logging(console=False)

            flask_thread = threading.Thread(
                target=flask_app.run_flask,
                daemon=True,
                name="flask",
            )
            flask_thread.start()
            flask_app.start_scheduler()
            logging.info("Manga Downloader service started — dashboard at http://localhost:8080")

            # Block until Windows Service Control Manager sends a stop signal
            win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
            logging.info("Manga Downloader service stopped.")
        except Exception as exc:
            logging.error(
                "Service crashed during startup: %s\n%s",
                exc,
                traceback.format_exc(),
            )
            raise


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(MangaDownloaderService)
