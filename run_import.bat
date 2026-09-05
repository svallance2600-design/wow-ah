@echo off
REM Import Auctionator scans into the archive and push, if anything changed.
REM
REM Safe to run on a schedule: if the SavedVariables file hasn't changed since
REM the last run, this writes nothing, commits nothing and exits quietly.
REM If the PC is off it simply doesn't run - nothing to clean up afterwards.
REM
REM Schedule it hourly (run once from an Administrator prompt):
REM
REM   schtasks /create /tn "WoW AH import" /sc hourly ^
REM     /tr "C:\Dev\Wow-ah\wowah\run_import.bat" /f
REM
REM Remove it again with:  schtasks /delete /tn "WoW AH import" /f

cd /d "%~dp0"
python import_auctionator.py --commit >> import.log 2>&1
