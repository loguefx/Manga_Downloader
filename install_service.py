"""
Helper script to install or uninstall the Manga Downloader Windows Service.

Must be run as Administrator.

  python install_service.py install
  python install_service.py uninstall
"""

import argparse
import subprocess
import sys
import os

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service.py")
PYTHON = sys.executable


def run(cmd: list) -> int:
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd)
    return result.returncode


def install():
    print("\n--- Installing Manga Downloader Windows Service ---\n")
    if run([PYTHON, SCRIPT, "--startup", "auto", "install"]) != 0:
        print("\nInstall failed. Make sure you are running as Administrator.")
        sys.exit(1)
    # pywin32 post-install step ensures the service can find Python DLLs
    post_install = os.path.join(os.path.dirname(PYTHON), "Scripts", "pywin32_postinstall.py")
    if os.path.exists(post_install):
        run([PYTHON, post_install, "-install"])

    run([PYTHON, SCRIPT, "start"])
    print("\nService installed and started.")
    print("Dashboard available at: http://localhost:8080")
    print("\nTo manage the service:")
    print("  python service.py stop    # stop")
    print("  python service.py start   # start")
    print("  python service.py restart # restart")
    print("  python install_service.py uninstall  # remove")


def uninstall():
    print("\n--- Removing Manga Downloader Windows Service ---\n")
    run([PYTHON, SCRIPT, "stop"])
    run([PYTHON, SCRIPT, "remove"])
    print("\nService removed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "uninstall"])
    args = parser.parse_args()

    if args.action == "install":
        install()
    else:
        uninstall()
