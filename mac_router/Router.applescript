on run
    set resourcePath to POSIX path of (path to resource "Dots2Codex")
    set commandPath to resourcePath & "/START.command"
    -- LaunchServices opens the executable .command in a real Terminal. No
    -- System Events, key simulation, accessibility permission or shell rc edits.
    do shell script "/usr/bin/open " & quoted form of commandPath
end run
