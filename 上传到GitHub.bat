@echo off
chcp 65001 >nul
cd /d "%~dp0"

set REPO_URL=git@github.com:xchsh2000/Youtube.git
set GIT_DIR_PATH=%~dp0.gitdata
set WORK_TREE_PATH=%~dp0
set SSH_KEY_PATH=%~dp0.ssh_github\youtube_github_ed25519
set KNOWN_HOSTS_PATH=%~dp0.ssh_github\known_hosts

echo.
echo 正在准备上传到 GitHub:
echo %REPO_URL%
echo.

git --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" remote set-url origin %REPO_URL% 2>nul
if errorlevel 1 git --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" remote add origin %REPO_URL%

git --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" config core.sshCommand "ssh -i \"%SSH_KEY_PATH%\" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=\"%KNOWN_HOSTS_PATH%\""

echo.
echo 请确认已经把下面这个公钥添加到 GitHub 仓库 Deploy keys，并勾选 Allow write access:
echo.
type "%SSH_KEY_PATH%.pub"
echo.
pause

echo.
echo 正在推送 main 分支...
git --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" push -u origin main

echo.
if errorlevel 1 (
  echo 上传失败。请把上面的错误信息发给 Codex。
) else (
  echo 上传完成：https://github.com/xchsh2000/Youtube
)
echo.
pause
