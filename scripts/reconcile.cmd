@echo off
rem 2026-09-24: replaced by the runner's (serve) own built-in schedule (08:30/18:15 stuck-job check).
rem This OS scheduled task once grabbed the runner's lease as a separate process and blocked the
rem 09:00 publish for an hour, so it is disabled here. Recommend disabling task scheduler V2R-Reconcile too.
exit /b 0
