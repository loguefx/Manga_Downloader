"""
Windows Service wrapper for the Manga Downloader.

Registers and runs the Flask web app + background scheduler as a Windows Service
so it starts automatically with Windows and survives reboots/logoffs.

Usage (run as Administrator):
  python service.py install    # install the service
  python service.py start      # start it
  python service.py stop       # stop it
  python service.py remove     # uninstall
  python service.py restart    # restart

Or use install_service.py for a guided install/uninstall.
"""

import os
import sys
import threading
import logging

# Add project directory to path so imports work when running as a service
_SVC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SVC_DIR not in sys.path:
    sys.path.insert(0, _SVC_DIR)

import win32service
import win32serviceutil
import win32event
import servicemanager


class MangaDownloaderService(win32serviceutil.ServiceFramework):
    _svc_name_         = "MangaDownloader"
    _svc_display_name_ = "Manga Downloader"
    _svc_description_  = (
        "Downloads manga from MangaDex and serves the web dashboard. "
        "Runs automatically in the background."
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._running = True

        logging.basicConfig(
            filename=os.path.join(_SVC_DIR, "manga_downloader.log"),
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
        import app as flask_app

        # Start Flask + scheduler in a daemon thread
        flask_thread = threading.Thread(
            target=flask_app.run_flask,
            daemon=True,
            name="flask",
        )
        flask_thread.start()
        flask_app.start_scheduler()
        logging.info("Manga Downloader service started.")

        # Block until the service is told to stop
        win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
        logging.info("Manga Downloader service stopped.")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(MangaDownloaderService)
