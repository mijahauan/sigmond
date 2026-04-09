#!/bin/bash

cat << 'BANNER'

          .---.
         / o o \     "Zo... ven did your
         \ ._. /      signals first start
          |||||       to propagate?"
         /|||||\             ______
        / ||||| \            \    /
       '  |||||  '            \  /
          (  )                 \/
       ~~smoke~~               |   /
                  -------------'  /
                  ---------------/
                  |            |
     Dr. SigMonD

    SigMonD v1.0 - Signal Monitor Daemon
    Usage: smd <command> [options]

    Commands:
      install    Install & configure components
      status     Show system status
      wspr       WSPR propagation monitoring
      grape      GRAPE time-signal recording
      config     Edit configuration
      log        View service logs
      update     Update to latest version
      diag       Run diagnostics

    Run 'smd <command> --help' for details.

BANNER
