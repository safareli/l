{ config, pkgs, lib, ... }:

let
  local = { username = "safareli"; homeDirectory = "/home/safareli.linux"; };
in
{
  # Home Manager needs a bit of information about you and the paths it should manage
  home.username = local.username;
  home.homeDirectory = local.homeDirectory;

  # This value determines the Home Manager release compatibility
  home.stateVersion = "24.11";

  # Let Home Manager install and manage itself
  programs.home-manager.enable = true;

  # ============================================================================
  # Packages
  # ============================================================================
  home.packages = with pkgs; [
    # Core development
    nodejs_25          # Latest (project flakes override with nodejs_22 LTS)
    bun

    # AI tools
    claude-code
    opencode
    pi

    # Editors
    vim
    nano

    # CLI tools
    jq
    gh                 # GitHub CLI
    fzf
    ripgrep
    fd
    bat                # better cat
    delta              # better diff pager
    eza                # better ls
    htop
    btop
    tree
    curl
    wget
    iputils            # ping, tracepath, etc.

    # Process management
    process-compose    # docker-compose for processes

    # Web servers
    darkhttpd          # tiny static file server

    # JSON viewers
    # otree            # Not in nixpkgs - install via: cargo install otree

    # Nix tools
    nil                # Nix LSP
    nixpkgs-fmt        # Nix formatter

    # Misc utilities
    time               # GNU time (more detailed than shell builtin)
  ];

  # ============================================================================
  # Shell - Zsh
  # ============================================================================
  programs.zsh = {
    enable = true;

    # Oh My Zsh
    oh-my-zsh = {
      enable = true;
      theme = "robbyrussell";
      plugins = [
        "git"
        "docker"
        "fzf"
        "z"              # directory jumping
        "history"
      ];
    };

    initContent = lib.mkMerge [
      (lib.mkBefore ''
        # Custom prompt with user@host prefix
        PROMPT="%F{green}%n@%m%f $PROMPT"

        # Git worktree username prefix
        export GW_USER="irakli"

        # PATH additions
        export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

        # fzf keybindings (Ctrl+R for history, Ctrl+T for files)
        if [ -n "''${commands[fzf-share]}" ]; then
          source "$(fzf-share)/key-bindings.zsh"
          source "$(fzf-share)/completion.zsh"
        fi

        # gw wrapper function to handle cd after worktree operations
        gw() {
          local cdfile=$(mktemp)
          trap "rm -f $cdfile" EXIT
          
          # Pass cdfile path to gw via env var - gw writes cd path there
          GW_CD_FILE="$cdfile" "$HOME/.local/bin/gw" "$@"
          local exit_code=$?
          
          # If cdfile has content, cd to it
          if [ -s "$cdfile" ]; then
            cd "$(cat "$cdfile")"
          fi
          
          return $exit_code
        }
      '')
      (lib.mkAfter ''
        # Lima BEGIN
        # Make sure iptables and mount.fuse3 are available
        PATH="$PATH:/usr/sbin:/sbin"
        export PATH
        # Lima END
      '')
    ];

    shellAliases = {
      ll = "eza -la";
      la = "eza -a";
      ls = "eza";
      cat = "bat";
      lg = "lazygit";
      cc = "claude --dangerously-skip-permissions";
      oc = "opencode";
      g = "git";

      # Nix shortcuts
      hms = "home-manager switch --flake ~/.config/home-manager";
      hmu = "~/.config/home-manager/scripts/update-versions.sh && nix flake update --flake ~/.config/home-manager && home-manager switch --flake ~/.config/home-manager";

      # GNU time with verbose output (use \time for non-verbose)
      time = "command time -v";

      # GitHub CI logs viewer
      gh-run-view = "gh-run-view";
    };
  };

  # ============================================================================
  # Direnv
  # ============================================================================
  programs.direnv = {
    enable = true;
    enableZshIntegration = true;
    nix-direnv.enable = true;  # Faster nix-shell/flake loading with caching
  };

  # ============================================================================
  # Git
  # ============================================================================
  programs.git = {
    enable = true;

    settings = {
      user.name = "Irakli Safareli";
      user.email = "i.safareli@gmail.com";
      init.defaultBranch = "main";
      push.autoSetupRemote = true;
      push.default = "current";
      pull.rebase = true;

      core = {
        autocrlf = "input";
        eol = "lf";
        trustctime = false;
        ignorecase = false;
        # editor = "cursor --wait";
      };

      pager = {
        diff = "delta --dark --paging=never --line-numbers";
        show = "delta --dark --paging=never --line-numbers";
        blame = "delta --dark --paging=never --line-numbers";
      };

      interactive = {
        diffFilter = "delta --dark --color-only";
      };

      delta = {
        navigate = true;
      };

      alias = {
        lg = "log --oneline --graph --decorate";
        fm = "fetch origin main:main";
        f = "fetch origin";
        fa = "fetch --all";
        r = "rebase origin/main";
        m = "merge origin/main";
        p = "push";
        s = "status";
        c = "checkout";
        cp = "cherry-pick";
        main = "checkout --detach main";
        todo = "!f() { git diff main... --unified=0 | grep '^+.*TODO'; }; f";
        cm = "commit";
        cn = "commit -n";
        ca = "commit --amend";
        n = ''!f() { git checkout -b irakli/$(date +%Y-%m-%d)-$1; }; f'';
        nm = ''!f() { git checkout -b irakli/$(date +%Y-%m-%d)-$1 main; }; f'';
        skipped = "!f() { git ls-files -v | grep ^S; }; f";
        skip = "update-index --skip-worktree";
        unskip = "update-index --no-skip-worktree";
        br = "branch --sort=-committerdate";
        up = "!git branch --set-upstream-to=$(git remote)/$(git branch --show-current) $(git branch --show-current)";
        fixup = ''!f() { TARGET=$(git rev-parse "$1"); git commit --fixup=$TARGET ''${@:2} && EDITOR=true git rebase -i --autostash --autosquash $TARGET^; }; f'';
        fixupn = ''!f() { TARGET=$(git rev-parse "$1"); git commit -n --fixup=$TARGET ''${@:2} && EDITOR=true git rebase -i --autostash --autosquash $TARGET^; }; f'';
      };
    };

    ignores = [
      ".pi"
    ];
  };

  # ============================================================================
  # Lazygit
  # ============================================================================
  programs.lazygit = {
    enable = true;
    settings = {
      git = {
        pagers = [
          {
            colorArg = "always";
            pager = "delta --dark --paging=never --line-numbers";
          }
        ];
      };
    };
  };

  # ============================================================================
  # tmux
  # ============================================================================
  programs.tmux = {
    enable = true;
    shortcut = "a";  # Use Ctrl+a as prefix (like screen)
    baseIndex = 1;   # Start windows at 1, not 0
    terminal = "screen-256color";
    historyLimit = 100000;
    escapeTime = 0;  # No delay for escape key
    mouse = true;    # Enable mouse support

    extraConfig = ''
      # Enable true color support
      set -ga terminal-overrides ",*256col*:Tc"

      # Split panes with | and -
      bind | split-window -h -c "#{pane_current_path}"
      bind - split-window -v -c "#{pane_current_path}"

      # New window in current path
      bind c new-window -c "#{pane_current_path}"

      # Reload config
      bind r source-file ~/.config/tmux/tmux.conf \; display "Config reloaded!"

      # Switch panes with Alt+arrow without prefix
      bind -n M-Left select-pane -L
      bind -n M-Right select-pane -R
      bind -n M-Up select-pane -U
      bind -n M-Down select-pane -D

      # Status bar styling
      set -g status-style 'bg=#333333 fg=#5eacd3'
      set -g status-left-length 50
      set -g status-right-length 80
      set -g status-right ' C-a ? help | C-a d detach | %H:%M '
    '';
  };

  # ============================================================================
  # fzf
  # ============================================================================
  programs.fzf = {
    enable = true;
    enableZshIntegration = true;
    defaultOptions = [
      "--height 40%"
      "--layout=reverse"
      "--border"
    ];
  };

  # ============================================================================
  # VS Code Server settings (for remote dev)
  # ============================================================================
  home.file.".vscode-server/data/Machine/settings.json".text = builtins.toJSON {
    "terminal.integrated.defaultProfile.linux" = "zsh";
    "remote.autoForwardPorts" = false;
  };

  # ============================================================================
  # AI Agent Skills (shared across all coding agents)
  # ============================================================================
  # Skills are stored in ~/.config/home-manager/skills/
  # and symlinked to each agent's expected location
  home.file.".pi/agent/skills".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/skills";
  home.file.".claude/skills".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/skills";
  home.file.".codex/skills".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/skills";

  # ============================================================================
  # Custom Scripts
  # ============================================================================
  # without this claude is confused it's like yo i'm definitly native install but why am i not where i think should be?
  home.file.".local/bin/claude" = {
    source = config.lib.file.mkOutOfStoreSymlink "${config.home.profileDirectory}/bin/claude";
  };

  home.file.".local/bin/user-systemd-status" = {
    text = ''
      #!/usr/bin/env bash
      systemctl status "user@$(id -u).service"
    '';
    executable = true;
  };

  home.file.".local/bin/user-systemd-restart" = {
    text = ''
      #!/usr/bin/env bash
      sudo systemctl restart "user@$(id -u).service"
    '';
    executable = true;
  };

  home.file.".local/bin/gw" = {
    source = ./scripts/gw;
    executable = true;
  };

  # View GitHub Actions logs with terminal colors (aliased as 'grv')
  home.file.".local/bin/gh-run-view" = {
    source = ./skills/gh-run-view/gh-run-view.sh;
    executable = true;
  };


  # ============================================================================
  # Portal - Local services index page
  # ============================================================================
  home.file.".local/share/portal/index.html".source = ./portal.html;

  # ============================================================================
  # Systemd user services
  # ============================================================================

  # # Check status
  # systemctl --user status opencode-web

  # # Start/stop/restart
  # systemctl --user start opencode-web 
  # systemctl --user stop opencode-web
  # systemctl --user restart opencode-web 

  # # View logs 
  # journalctl --user -u opencode-web -f

systemd.user.services.opencode-web = {
    Unit = {
      Description = "OpenCode Web UI";
      After = [ "network.target" ];
    };
    Service = {
      EnvironmentFile = "-%h/.config/home-manager/.env";
      ExecStart = "${pkgs.opencode}/bin/opencode web --port 6767 --hostname=0.0.0.0";
      StandardOutput = "append:%h/.local/share/opencode-web/opencode-web.log";
      StandardError = "append:%h/.local/share/opencode-web/opencode-web.log";
      Restart = "on-failure";
      RestartSec = 5;
    };
    Install = {
      WantedBy = [ "default.target" ];
    };
  };

  systemd.user.services.tts-server = {
    Unit = {
      Description = "Piper TTS Server";
      After = [ "network.target" ];
    };
    Service = {
      WorkingDirectory = "%h/dev/tts";
      ExecStart = "%h/dev/tts/run_server.sh --host 0.0.0.0 --port 6768";
      StandardOutput = "append:%h/.local/share/tts-server/tts-server.log";
      StandardError = "append:%h/.local/share/tts-server/tts-server.log";
      Restart = "on-failure";
      RestartSec = 5;
    };
    Install = {
      WantedBy = [ "default.target" ];
    };
  };

  systemd.user.services.portal = {
    Unit = {
      Description = "Portal - Local services index";
      After = [ "network.target" ];
    };
    Service = {
      ExecStart = "${pkgs.darkhttpd}/bin/darkhttpd %h/.local/share/portal --port 1111 --addr 0.0.0.0";
      StandardOutput = "append:%h/.local/share/portal/portal.log";
      StandardError = "append:%h/.local/share/portal/portal.log";
      Restart = "on-failure";
      RestartSec = 5;
    };
    Install = {
      WantedBy = [ "default.target" ];
    };
  };

  systemd.user.services.ttyd = {
    Unit = {
      Description = "ttyd - Web terminal";
      After = [ "network.target" ];
    };
    Service = {
      ExecStart = "${pkgs.ttyd}/bin/ttyd -W -p 6769 -t scrollback=100000 ${pkgs.zsh}/bin/zsh";
      StandardOutput = "append:%h/.local/share/ttyd/ttyd.log";
      StandardError = "append:%h/.local/share/ttyd/ttyd.log";
      Restart = "on-failure";
      RestartSec = 5;
    };
    Install = {
      WantedBy = [ "default.target" ];
    };
  };
}
