const vscode = require("vscode");
const { execFile } = require("child_process");

let output;

function run(args, cwd) {
  const exe = vscode.workspace.getConfiguration("gitgraph").get("executable", "gitgraph");
  return new Promise((resolve, reject) => {
    execFile(exe, args, { cwd, maxBuffer: 32 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        reject(new Error((stderr || err.message).trim()));
      } else {
        resolve(stdout);
      }
    });
  });
}

function workspaceRoot() {
  const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  if (!folder) {
    throw new Error("Open a folder containing a Git repository first.");
  }
  return folder.uri.fsPath;
}

function currentRelativePath() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    throw new Error("Open a file first.");
  }
  return vscode.workspace.asRelativePath(editor.document.uri, false);
}

async function show(args) {
  const text = await run(args, workspaceRoot());
  output.clear();
  output.appendLine(text);
  output.show(true);
}

function command(fn) {
  return async () => {
    try {
      await fn();
    } catch (e) {
      vscode.window.showErrorMessage(`GitGraph: ${e.message}`);
    }
  };
}

function activate(context) {
  output = vscode.window.createOutputChannel("GitGraph");
  const reg = (id, fn) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, command(fn)));
  reg("gitgraph.index", async () => {
    await run(["index"], workspaceRoot());
    vscode.window.showInformationMessage("GitGraph: repository indexed.");
  });
  reg("gitgraph.context", async () => {
    const task = await vscode.window.showInputBox({ prompt: "Describe the task" });
    if (task) {
      await show(["context", task]);
    }
  });
  reg("gitgraph.impact", () => show(["impact", currentRelativePath()]));
  reg("gitgraph.history", () => show(["history", currentRelativePath()]));
  context.subscriptions.push(output);
}

function deactivate() {}

module.exports = { activate, deactivate };
