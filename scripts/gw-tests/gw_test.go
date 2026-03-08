package gwtests

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"testing"
	"time"
)

type cmdResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
	Err      error
}

func (r cmdResult) combined() string {
	return r.Stdout + r.Stderr
}

type repoFixture struct {
	Root   string
	Home   string
	Bin    string
	Origin string
	Main   string
	Script string
}

func newRepoFixture(t *testing.T) *repoFixture {
	t.Helper()

	script := gwScriptPath(t)
	root := t.TempDir()
	home := filepath.Join(root, "home")
	bin := filepath.Join(root, "bin")
	seed := filepath.Join(root, "seed")
	origin := filepath.Join(root, "origin.git")
	main := filepath.Join(root, "main")

	mustMkdirAll(t, home)
	mustMkdirAll(t, bin)
	mustMkdirAll(t, seed)

	f := &repoFixture{
		Root:   root,
		Home:   home,
		Bin:    bin,
		Origin: origin,
		Main:   main,
		Script: script,
	}

	f.git(t, seed, nil, "init", "-b", "main")
	f.git(t, seed, nil, "config", "user.name", "GW Tests")
	f.git(t, seed, nil, "config", "user.email", "gw-tests@example.com")
	mustWriteFile(t, filepath.Join(seed, "README.md"), "hello\n")
	f.git(t, seed, nil, "add", "README.md")
	f.git(t, seed, nil, "commit", "-m", "initial")

	f.git(t, root, nil, "clone", "--bare", seed, origin)
	f.git(t, root, nil, "clone", origin, main)
	f.git(t, main, nil, "config", "user.name", "GW Tests")
	f.git(t, main, nil, "config", "user.email", "gw-tests@example.com")

	return f
}

func gwScriptPath(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	path := filepath.Clean(filepath.Join(filepath.Dir(file), "..", "gw"))
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("gw script not found at %s: %v", path, err)
	}
	return path
}

func (f *repoFixture) baseEnv(extra map[string]string) map[string]string {
	env := map[string]string{
		"HOME":                f.Home,
		"USER":                "tester",
		"PATH":                f.Bin + ":" + os.Getenv("PATH"),
		"GIT_TERMINAL_PROMPT": "0",
		"LC_ALL":              "C",
		"TZ":                  "UTC",
	}
	for k, v := range extra {
		env[k] = v
	}
	return env
}

func (f *repoFixture) gw(t *testing.T, cwd, stdin string, extraEnv map[string]string, args ...string) cmdResult {
	t.Helper()
	fullArgs := append([]string{f.Script}, args...)
	return runCommand(t, cwd, f.baseEnv(extraEnv), stdin, "bash", fullArgs...)
}

func (f *repoFixture) git(t *testing.T, cwd string, extraEnv map[string]string, args ...string) string {
	t.Helper()
	res := runCommand(t, cwd, f.baseEnv(extraEnv), "", "git", args...)
	if res.ExitCode != 0 {
		t.Fatalf("git %v failed (exit=%d)\nstdout:\n%s\nstderr:\n%s", args, res.ExitCode, res.Stdout, res.Stderr)
	}
	return strings.TrimSpace(res.Stdout)
}

func (f *repoFixture) pushCommitToOrigin(t *testing.T, filename, content, message string) string {
	t.Helper()

	updater := filepath.Join(f.Root, fmt.Sprintf("updater-%d", time.Now().UnixNano()))
	f.git(t, f.Root, nil, "clone", f.Origin, updater)
	f.git(t, updater, nil, "config", "user.name", "GW Tests")
	f.git(t, updater, nil, "config", "user.email", "gw-tests@example.com")

	mustWriteFile(t, filepath.Join(updater, filename), content)
	f.git(t, updater, nil, "add", filename)
	f.git(t, updater, nil, "commit", "-m", message)
	f.git(t, updater, nil, "push", "origin", "main")

	return f.git(t, updater, nil, "rev-parse", "HEAD")
}

func (f *repoFixture) createRemoteOnlyBranch(t *testing.T, branch string) {
	t.Helper()

	f.git(t, f.Main, nil, "checkout", "-b", branch)
	mustWriteFile(t, filepath.Join(f.Main, "remote-branch.txt"), "remote only\n")
	f.git(t, f.Main, nil, "add", "remote-branch.txt")
	f.git(t, f.Main, nil, "commit", "-m", "remote branch commit")
	f.git(t, f.Main, nil, "push", "-u", "origin", branch)
	f.git(t, f.Main, nil, "checkout", "main")
	f.git(t, f.Main, nil, "branch", "-D", branch)
	f.git(t, f.Main, nil, "fetch", "origin")
}

func (f *repoFixture) writeStub(t *testing.T, name, script string) {
	t.Helper()
	path := filepath.Join(f.Bin, name)
	mustWriteFileMode(t, path, script, 0o755)
}

func runCommand(t *testing.T, cwd string, env map[string]string, stdin string, name string, args ...string) cmdResult {
	t.Helper()

	cmd := exec.Command(name, args...)
	cmd.Dir = cwd
	cmd.Env = mergeEnv(env)

	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}

	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	exitCode := 0
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		} else {
			t.Fatalf("failed to run %s %v: %v", name, args, err)
		}
	}

	return cmdResult{
		Stdout:   stdout.String(),
		Stderr:   stderr.String(),
		ExitCode: exitCode,
		Err:      err,
	}
}

func mergeEnv(overrides map[string]string) []string {
	envMap := map[string]string{}
	for _, kv := range os.Environ() {
		parts := strings.SplitN(kv, "=", 2)
		if len(parts) == 2 {
			envMap[parts[0]] = parts[1]
		}
	}
	for k, v := range overrides {
		envMap[k] = v
	}

	out := make([]string, 0, len(envMap))
	for k, v := range envMap {
		out = append(out, k+"="+v)
	}
	sort.Strings(out)
	return out
}

func mustMkdirAll(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
}

func mustWriteFile(t *testing.T, path, content string) {
	t.Helper()
	mustWriteFileMode(t, path, content, 0o644)
}

func mustWriteFileMode(t *testing.T, path, content string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir for %s: %v", path, err)
	}
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func mustReadFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(data)
}

func requireSuccess(t *testing.T, res cmdResult) {
	t.Helper()
	if res.ExitCode != 0 {
		t.Fatalf("expected success, got exit=%d\nstdout:\n%s\nstderr:\n%s", res.ExitCode, res.Stdout, res.Stderr)
	}
}

func requireFailure(t *testing.T, res cmdResult) {
	t.Helper()
	if res.ExitCode == 0 {
		t.Fatalf("expected failure, got exit=0\nstdout:\n%s\nstderr:\n%s", res.Stdout, res.Stderr)
	}
}

func parseWorktreePathFromOutput(t *testing.T, output string) string {
	t.Helper()
	re := regexp.MustCompile(`(?m)Worktree ready: (.+)$`)
	m := re.FindStringSubmatch(output)
	if len(m) != 2 {
		t.Fatalf("could not parse worktree path from output:\n%s", output)
	}
	return strings.TrimSpace(m[1])
}

func TestHelpAndUnknownCommand(t *testing.T) {
	f := newRepoFixture(t)

	help := f.gw(t, f.Main, "", nil, "help")
	requireSuccess(t, help)
	if !strings.Contains(help.Stdout, "Git worktree manager") {
		t.Fatalf("help output missing expected header:\n%s", help.Stdout)
	}

	unknown := f.gw(t, f.Main, "", nil, "does-not-exist")
	requireFailure(t, unknown)
	if !strings.Contains(unknown.combined(), "Unknown command") {
		t.Fatalf("expected unknown command error, got:\n%s", unknown.combined())
	}
}

func TestShellHookVariants(t *testing.T) {
	f := newRepoFixture(t)

	bashHook := f.gw(t, f.Main, "", nil, "shell-hook", "bash")
	requireSuccess(t, bashHook)
	if !strings.Contains(bashHook.Stdout, "GW_EVAL_FILE") || !strings.Contains(bashHook.Stdout, "source") {
		t.Fatalf("bash hook output unexpected:\n%s", bashHook.Stdout)
	}

	zshHook := f.gw(t, f.Main, "", nil, "shell-hook", "zsh")
	requireSuccess(t, zshHook)
	if !strings.Contains(zshHook.Stdout, "gw() {") {
		t.Fatalf("zsh hook output unexpected:\n%s", zshHook.Stdout)
	}

	invalid := f.gw(t, f.Main, "", nil, "shell-hook", "fish")
	requireFailure(t, invalid)
	if !strings.Contains(invalid.combined(), "Unsupported shell") {
		t.Fatalf("expected unsupported shell error, got:\n%s", invalid.combined())
	}
}

func TestNewValidationAndExistingDirectory(t *testing.T) {
	f := newRepoFixture(t)

	missing := f.gw(t, f.Main, "", nil, "new")
	requireFailure(t, missing)
	if !strings.Contains(missing.combined(), "Branch name required") {
		t.Fatalf("expected missing branch error, got:\n%s", missing.combined())
	}

	tooMany := f.gw(t, f.Main, "", nil, "new", "a", "b")
	requireFailure(t, tooMany)
	if !strings.Contains(tooMany.combined(), "accepts only one argument") {
		t.Fatalf("expected too many args error, got:\n%s", tooMany.combined())
	}

	f.git(t, f.Main, nil, "branch", "topic", "origin/main")
	mustMkdirAll(t, filepath.Join(f.Root, "main--topic"))

	exists := f.gw(t, f.Main, "", nil, "new", "topic")
	requireFailure(t, exists)
	if !strings.Contains(exists.combined(), "Directory already exists") {
		t.Fatalf("expected existing directory error, got:\n%s", exists.combined())
	}
}

func TestNewCreatesGeneratedBranchAndWritesEvalCd(t *testing.T) {
	f := newRepoFixture(t)
	evalFile := filepath.Join(t.TempDir(), "gw-eval.sh")

	res := f.gw(t, f.Main, "", map[string]string{
		"GW_USER":      "alice",
		"GW_EVAL_FILE": evalFile,
	}, "new", "feature-auth")
	requireSuccess(t, res)

	evalContent := strings.TrimSpace(mustReadFile(t, evalFile))
	if !strings.HasPrefix(evalContent, "cd ") {
		t.Fatalf("expected eval file to contain cd command, got: %q", evalContent)
	}
	worktreePath := strings.TrimPrefix(evalContent, "cd ")
	if _, err := os.Stat(worktreePath); err != nil {
		t.Fatalf("expected worktree dir %s to exist: %v", worktreePath, err)
	}

	branch := f.git(t, worktreePath, nil, "branch", "--show-current")
	if ok, _ := regexp.MatchString(`^alice/[0-9]{4}-[0-9]{2}-[0-9]{2}-feature-auth$`, branch); !ok {
		t.Fatalf("unexpected generated branch name: %q", branch)
	}
	if !strings.Contains(res.combined(), "Worktree ready:") {
		t.Fatalf("expected success output to include worktree path, got:\n%s", res.combined())
	}
}

func TestNewUsesExistingLocalBranch(t *testing.T) {
	f := newRepoFixture(t)
	f.git(t, f.Main, nil, "branch", "topic/local", "origin/main")

	res := f.gw(t, f.Main, "", nil, "new", "topic/local")
	requireSuccess(t, res)

	wt := filepath.Join(f.Root, "main--topic-local")
	if _, err := os.Stat(wt); err != nil {
		t.Fatalf("expected %s to exist: %v", wt, err)
	}

	branch := f.git(t, wt, nil, "branch", "--show-current")
	if branch != "topic/local" {
		t.Fatalf("expected worktree branch topic/local, got %q", branch)
	}
}

func TestNewUsesExistingRemoteBranch(t *testing.T) {
	f := newRepoFixture(t)
	f.createRemoteOnlyBranch(t, "remote-only")

	res := f.gw(t, f.Main, "", nil, "new", "remote-only")
	requireSuccess(t, res)

	wt := filepath.Join(f.Root, "main--remote-only")
	if _, err := os.Stat(wt); err != nil {
		t.Fatalf("expected %s to exist: %v", wt, err)
	}

	branch := f.git(t, wt, nil, "branch", "--show-current")
	if branch != "remote-only" {
		t.Fatalf("expected branch remote-only, got %q", branch)
	}

	localHash := f.git(t, f.Main, nil, "rev-parse", "remote-only")
	remoteHash := f.git(t, f.Main, nil, "rev-parse", "origin/remote-only")
	if localHash != remoteHash {
		t.Fatalf("local branch hash %s != remote hash %s", localHash, remoteHash)
	}
}

func TestNewBranchDoesNotTrackOriginMain(t *testing.T) {
	f := newRepoFixture(t)

	res := f.gw(t, f.Main, "", map[string]string{"GW_USER": "alice"}, "new", "no-track-feature")
	requireSuccess(t, res)

	wt := parseWorktreePathFromOutput(t, res.combined())
	branch := f.git(t, wt, nil, "branch", "--show-current")

	// New branches should NOT track origin/main
	remoteRes := runCommand(t, wt, f.baseEnv(nil), "", "git", "config", "--get", "branch."+branch+".remote")
	if remoteRes.ExitCode == 0 {
		t.Fatalf("new branch %q should not have upstream tracking, but has remote=%q", branch, strings.TrimSpace(remoteRes.Stdout))
	}
}

func TestNewFromExistingRemoteBranchTracksRemote(t *testing.T) {
	f := newRepoFixture(t)
	f.createRemoteOnlyBranch(t, "existing-remote")

	res := f.gw(t, f.Main, "", nil, "new", "existing-remote")
	requireSuccess(t, res)

	wt := filepath.Join(f.Root, "main--existing-remote")
	branch := f.git(t, wt, nil, "branch", "--show-current")
	if branch != "existing-remote" {
		t.Fatalf("expected branch existing-remote, got %q", branch)
	}

	// Branches from existing remote refs SHOULD track their remote counterpart
	remote := f.git(t, f.Main, nil, "config", "--get", "branch.existing-remote.remote")
	if remote != "origin" {
		t.Fatalf("expected remote tracking to be 'origin', got %q", remote)
	}
	merge := f.git(t, f.Main, nil, "config", "--get", "branch.existing-remote.merge")
	if merge != "refs/heads/existing-remote" {
		t.Fatalf("expected merge tracking to be refs/heads/existing-remote, got %q", merge)
	}
}

func TestNewFallsBackToLocalMainWithoutOrigin(t *testing.T) {
	f := newRepoFixture(t)

	// Remove origin remote
	f.git(t, f.Main, nil, "remote", "remove", "origin")

	res := f.gw(t, f.Main, "", map[string]string{"GW_USER": "alice"}, "new", "local-feature")
	requireSuccess(t, res)

	wt := parseWorktreePathFromOutput(t, res.combined())
	branch := f.git(t, wt, nil, "branch", "--show-current")

	if ok, _ := regexp.MatchString(`^alice/[0-9]{4}-[0-9]{2}-[0-9]{2}-local-feature$`, branch); !ok {
		t.Fatalf("unexpected generated branch name: %q", branch)
	}

	// Verify the new branch points to same commit as main
	mainHash := f.git(t, f.Main, nil, "rev-parse", "main")
	branchHash := f.git(t, wt, nil, "rev-parse", "HEAD")
	if mainHash != branchHash {
		t.Fatalf("expected branch to start at main (%s), got %s", mainHash, branchHash)
	}
}

func TestNewCopiesEnvAndPiAndRegeneratesBranchHash(t *testing.T) {
	f := newRepoFixture(t)

	mustWriteFile(t, filepath.Join(f.Main, ".env"), "FOO=bar\nBRANCH_HASH=deadbeef\nBAR=baz\n")
	mustWriteFile(t, filepath.Join(f.Main, ".pi", "settings.json"), `{"ok":true}`)

	res := f.gw(t, f.Main, "", map[string]string{"GW_USER": "copy"}, "new", "copy-env")
	requireSuccess(t, res)

	wt := parseWorktreePathFromOutput(t, res.combined())
	envContent := mustReadFile(t, filepath.Join(wt, ".env"))

	if !strings.Contains(envContent, "FOO=bar") || !strings.Contains(envContent, "BAR=baz") {
		t.Fatalf("copied env missing expected vars:\n%s", envContent)
	}
	if strings.Contains(envContent, "BRANCH_HASH=deadbeef") {
		t.Fatalf("old BRANCH_HASH should be removed:\n%s", envContent)
	}
	if ok, _ := regexp.MatchString(`(?m)^BRANCH_HASH=[0-9a-f]{8}$`, envContent); !ok {
		t.Fatalf("expected regenerated BRANCH_HASH in env file:\n%s", envContent)
	}
	if !strings.Contains(envContent, "# Auto-generated for worktree isolation") {
		t.Fatalf("expected auto-generated comment in env:\n%s", envContent)
	}

	if _, err := os.Stat(filepath.Join(wt, ".pi", "settings.json")); err != nil {
		t.Fatalf("expected .pi dir to be copied: %v", err)
	}
}

func TestDeleteSafetyAndAbort(t *testing.T) {
	f := newRepoFixture(t)

	mainDelete := f.gw(t, f.Main, "", nil, "delete")
	requireFailure(t, mainDelete)
	if !strings.Contains(mainDelete.combined(), "Cannot delete main worktree") {
		t.Fatalf("expected main deletion safety error, got:\n%s", mainDelete.combined())
	}

	f.git(t, f.Main, nil, "branch", "topic", "origin/main")
	create := f.gw(t, f.Main, "", nil, "new", "topic")
	requireSuccess(t, create)

	wt := filepath.Join(f.Root, "main--topic")
	abort := f.gw(t, wt, "n\n", nil, "delete")
	requireFailure(t, abort)
	if !strings.Contains(abort.combined(), "Aborted") {
		t.Fatalf("expected abort message, got:\n%s", abort.combined())
	}
	if _, err := os.Stat(wt); err != nil {
		t.Fatalf("worktree should still exist after abort: %v", err)
	}
}

func TestDeleteCurrentWorktreeConfirmedWritesCd(t *testing.T) {
	f := newRepoFixture(t)

	f.git(t, f.Main, nil, "branch", "topic", "origin/main")
	create := f.gw(t, f.Main, "", nil, "new", "topic")
	requireSuccess(t, create)

	wt := filepath.Join(f.Root, "main--topic")
	evalFile := filepath.Join(t.TempDir(), "eval.sh")

	deleted := f.gw(t, wt, "y\n", map[string]string{"GW_EVAL_FILE": evalFile}, "delete")
	requireSuccess(t, deleted)

	if _, err := os.Stat(wt); !os.IsNotExist(err) {
		t.Fatalf("expected worktree to be removed, stat err=%v", err)
	}

	evalContent := mustReadFile(t, evalFile)
	expectedCD := "cd " + f.Main
	if !strings.Contains(evalContent, expectedCD) {
		t.Fatalf("expected eval file to contain %q, got:\n%s", expectedCD, evalContent)
	}

	list := f.git(t, f.Main, nil, "worktree", "list")
	if strings.Contains(list, wt) {
		t.Fatalf("worktree still present in git worktree list:\n%s", list)
	}
}

func TestDeleteByBranchLookupForCustomWorktreePath(t *testing.T) {
	f := newRepoFixture(t)

	f.git(t, f.Main, nil, "branch", "topic", "origin/main")
	custom := filepath.Join(f.Root, "custom-topic-worktree")
	f.git(t, f.Main, nil, "worktree", "add", custom, "topic")

	deleted := f.gw(t, f.Main, "y\n", nil, "delete", "topic")
	requireSuccess(t, deleted)
	if _, err := os.Stat(custom); !os.IsNotExist(err) {
		t.Fatalf("expected custom worktree to be removed, stat err=%v", err)
	}
}

func TestListShowsGoldenAndDirty(t *testing.T) {
	f := newRepoFixture(t)

	f.git(t, f.Main, nil, "branch", "topic", "origin/main")
	wt := filepath.Join(f.Root, "main--topic")
	f.git(t, f.Main, nil, "worktree", "add", wt, "topic")
	mustWriteFile(t, filepath.Join(wt, "README.md"), "dirty change\n")

	f.git(t, f.Main, nil, "config", "--local", "gw.golden", "true")

	res := f.gw(t, f.Main, "", nil, "list")
	requireSuccess(t, res)
	out := res.combined()

	if !strings.Contains(out, "[golden]") {
		t.Fatalf("expected golden marker in list output:\n%s", out)
	}
	if !strings.Contains(out, "main--topic") {
		t.Fatalf("expected worktree name in list output:\n%s", out)
	}
	if !strings.Contains(out, "[dirty]") {
		t.Fatalf("expected dirty marker in list output:\n%s", out)
	}
}

func TestInitGoldenAndIdempotent(t *testing.T) {
	f := newRepoFixture(t)

	initRes := f.gw(t, f.Main, "", nil, "init-golden")
	requireSuccess(t, initRes)

	golden := f.git(t, f.Main, nil, "config", "--local", "--get", "gw.golden")
	if golden != "true" {
		t.Fatalf("expected gw.golden=true, got %q", golden)
	}

	headName := f.git(t, f.Main, nil, "rev-parse", "--abbrev-ref", "HEAD")
	if headName != "HEAD" {
		t.Fatalf("expected detached HEAD after init-golden, got %q", headName)
	}

	exclude := mustReadFile(t, filepath.Join(f.Main, ".git", "info", "exclude"))
	if !strings.Contains(exclude, ".pnpm-store") {
		t.Fatalf("expected .pnpm-store in .git/info/exclude, got:\n%s", exclude)
	}

	again := f.gw(t, f.Main, "", nil, "init-golden")
	requireSuccess(t, again)
	if !strings.Contains(again.combined(), "Already a golden repo") {
		t.Fatalf("expected idempotent init message, got:\n%s", again.combined())
	}
}

func TestInitGoldenFailsFromLinkedWorktree(t *testing.T) {
	f := newRepoFixture(t)

	f.git(t, f.Main, nil, "branch", "topic", "origin/main")
	wt := filepath.Join(f.Root, "main--topic")
	f.git(t, f.Main, nil, "worktree", "add", wt, "topic")

	res := f.gw(t, wt, "", nil, "init-golden")
	requireFailure(t, res)
	if !strings.Contains(res.combined(), "Run init-golden from the base repo") {
		t.Fatalf("expected init-golden linked-worktree error, got:\n%s", res.combined())
	}
}

func TestUpdateGoldenRequiresGoldenRepo(t *testing.T) {
	f := newRepoFixture(t)

	res := f.gw(t, f.Main, "", nil, "update-golden")
	requireFailure(t, res)
	if !strings.Contains(res.combined(), "Not a golden repo") {
		t.Fatalf("expected not golden error, got:\n%s", res.combined())
	}
}

func TestUpdateGoldenNoOpWhenUpToDate(t *testing.T) {
	f := newRepoFixture(t)
	requireSuccess(t, f.gw(t, f.Main, "", nil, "init-golden"))

	res := f.gw(t, f.Main, "", nil, "update-golden")
	requireSuccess(t, res)
	if !strings.Contains(res.combined(), "Golden already up to date") {
		t.Fatalf("expected no-op update message, got:\n%s", res.combined())
	}
}

func TestUpdateGoldenFetchesAndMovesHead(t *testing.T) {
	f := newRepoFixture(t)
	requireSuccess(t, f.gw(t, f.Main, "", nil, "init-golden"))

	before := f.git(t, f.Main, nil, "rev-parse", "HEAD")
	remoteHead := f.pushCommitToOrigin(t, "upstream.txt", "fresh upstream\n", "upstream update")

	res := f.gw(t, f.Main, "", nil, "update-golden")
	requireSuccess(t, res)

	after := f.git(t, f.Main, nil, "rev-parse", "HEAD")
	if before == after {
		t.Fatalf("expected HEAD to move after update, still at %s", after)
	}
	if after != remoteHead {
		t.Fatalf("expected HEAD %s to match remote commit %s", after, remoteHead)
	}
	if !strings.Contains(res.combined(), "Golden updated to") {
		t.Fatalf("expected update completion message, got:\n%s", res.combined())
	}
}

func TestUpdateGoldenStashesAndRestoresDirtyOverlayWorktrees(t *testing.T) {
	f := newRepoFixture(t)
	requireSuccess(t, f.gw(t, f.Main, "", nil, "init-golden"))

	f.git(t, f.Main, nil, "branch", "topic", "origin/main")
	wt := filepath.Join(f.Root, "main--topic")
	f.git(t, f.Main, nil, "worktree", "add", wt, "topic")

	mustWriteFile(t, filepath.Join(wt, "README.md"), "locally dirty\n")
	mustWriteFile(t, filepath.Join(wt, "untracked.txt"), "local file\n")

	f.pushCommitToOrigin(t, "remote-change.txt", "remote update\n", "remote update")

	f.writeStub(t, "findmnt", `#!/usr/bin/env bash
set -euo pipefail
	target=""
	while [ $# -gt 0 ]; do
	  if [ "$1" = "--target" ] && [ $# -gt 1 ]; then
	    target="$2"
	    break
	  fi
	  shift
	done

	if [[ "$target" == *"--"* ]]; then
	  echo "overlay"
	  exit 0
	fi

	exit 1
`)

	res := f.gw(t, f.Main, "y\n", nil, "update-golden")
	requireSuccess(t, res)
	out := res.combined()
	if !strings.Contains(out, "Stashing:") || !strings.Contains(out, "Unstashing:") {
		t.Fatalf("expected stash/unstash logs, got:\n%s", out)
	}

	status := f.git(t, wt, nil, "status", "--porcelain")
	if !strings.Contains(status, "README.md") || !strings.Contains(status, "untracked.txt") {
		t.Fatalf("expected dirty files to be restored after update, got:\n%s", status)
	}
}
