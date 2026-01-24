{ config, pkgs, lib, ... }:

{
  # Home Manager needs a bit of information about you and the paths it should manage
  home.username = "safareli";
  home.homeDirectory = "/home/safareli";

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
    claude-code-bun    # from github:sadjow/claude-code-nix (faster, uses Bun)
    opencode           # from github:anomalyco/opencode (pinned to v1.1.34)
    pi                 # from github:badlogic/pi-mono (pinned to v0.49.3)

    # CLI tools
    jq
    lazygit
    fzf
    ripgrep
    fd
    bat                # better cat
    eza                # better ls
    htop
    tree
    curl
    wget

    # Process management
    process-compose    # docker-compose for processes

    # JSON viewers
    # otree            # Not in nixpkgs - install via: cargo install otree

    # Nix tools
    nil                # Nix LSP
    nixpkgs-fmt        # Nix formatter
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

    initContent = ''
      # Custom prompt with user@host prefix
      PROMPT="%F{green}%n@%m%f $PROMPT"

      # PATH additions
      export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

      # fzf keybindings (Ctrl+R for history, Ctrl+T for files)
      if [ -n "''${commands[fzf-share]}" ]; then
        source "$(fzf-share)/key-bindings.zsh"
        source "$(fzf-share)/completion.zsh"
      fi
    '';

    shellAliases = {
      ll = "eza -la";
      la = "eza -a";
      ls = "eza";
      cat = "bat";
      lg = "lazygit";
      claude = "claude-bun";
      g = "git";

      # Nix shortcuts
      hms = "home-manager switch --flake ~/.config/home-manager";
      hmu = "nix flake update ~/.config/home-manager && home-manager switch --flake ~/.config/home-manager";
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

      alias = {
        lg = "log --oneline --graph --decorate";
        fm = "fetch origin main:main";
        f = "fetch --all";
        r = "rebase origin/main";
        m = "merge origin/main";
        p = "push";
        s = "status";
        c = "checkout";
        cp = "cherry-pick";
        cm = "checkout --detach main";
        todo = "!f() { git diff main... --unified=0 | grep '^+.*TODO'; }; f";
        cc = "commit";
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

    # ignores = [
    #   ".DS_Store"
    # ];
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
  };


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
      EnvironmentFile = "%h/.config/home-manager/.env";
      ExecStart = "${pkgs.opencode}/bin/opencode web --port 6767 --hostname=0.0.0.0";
      Restart = "on-failure";
      RestartSec = 5;
    };
    Install = {
      WantedBy = [ "default.target" ];
    };
  };
}
