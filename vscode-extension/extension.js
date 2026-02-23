// McCode VS Code extension – launches mclsp as a stdio language server.
// Automatically installs mclsp via pip if it is not already available.

const vscode = require('vscode');
const { LanguageClient, TransportKind } = require('vscode-languageclient/node');
const { execFile } = require('child_process');
const { promisify } = require('util');

const execFileAsync = promisify(execFile);
let client;

// ---------------------------------------------------------------------------
// Virtual C document provider
// ---------------------------------------------------------------------------
// Provides read-only C documents under the mccode-c:// scheme.  The content
// is fetched from the mclsp server via a custom request so that clangd (or
// VS Code's built-in C language features) can analyse the stitched C source.

class McCodeVirtualCProvider {
  constructor() {
    // Cache: virtualUri (string) → content (string)
    this._cache = new Map();
    // EventEmitter for onDidChange
    this._emitter = new vscode.EventEmitter();
    this.onDidChange = this._emitter.event;
  }

  /** Called by VS Code when it needs the content of a mccode-c:// document. */
  provideTextDocumentContent(uri) {
    return this._cache.get(uri.toString()) ?? '/* loading… */';
  }

  /** Update cached content and fire a change event so VS Code re-reads it. */
  update(virtualUriString, content) {
    this._cache.set(virtualUriString, content);
    this._emitter.fire(vscode.Uri.parse(virtualUriString));
  }

  /** Remove cached content when the McCode document is closed. */
  remove(virtualUriString) {
    this._cache.delete(virtualUriString);
  }
}

const virtualCProvider = new McCodeVirtualCProvider();

// ---------------------------------------------------------------------------
// Server discovery and auto-install
// ---------------------------------------------------------------------------

/** Try running `cmd` with a single argument; return true if it doesn't ENOENT. */
async function commandExists(cmd) {
  try {
    await execFileAsync(cmd, ['--version'], { timeout: 5000 });
    return true;
  } catch (e) {
    // ENOENT / EACCES → not found.  Any other error (bad exit code) → found.
    return e.code !== 'ENOENT' && e.code !== 'EACCES';
  }
}

/** Return a Python 3.10+ interpreter path, or null if none found.
 *  Checks: mccode.pythonPath setting → VS Code Python extension → python3 → python.
 */
async function findPython() {
  // 1. Explicit setting
  const config = vscode.workspace.getConfiguration('mccode');
  const configuredPython = (config.get('pythonPath') || '').trim();
  if (configuredPython && await commandExists(configuredPython)) return configuredPython;

  // 2. VS Code Python extension active interpreter.
  const pyExt = vscode.extensions.getExtension('ms-python.python');
  if (pyExt) {
    try {
      await pyExt.activate();
      const interp =
        pyExt.exports?.settings?.getExecutionDetails?.()?.execCommand?.[0] ||
        pyExt.exports?.environments?.getActiveEnvironmentPath?.()?.path;
      if (interp && await commandExists(interp)) return interp;
    } catch (_) { /* ignore */ }
  }

  // Fall back to PATH candidates.
  for (const candidate of ['python3', 'python']) {
    try {
      const { stdout } = await execFileAsync(candidate, ['--version'], { timeout: 5000 });
      // stdout: "Python 3.x.y" — require >= 3.10
      const m = stdout.match(/Python 3\.(\d+)/);
      if (m && parseInt(m[1], 10) >= 10) return candidate;
    } catch (_) { /* not found or wrong version */ }
  }
  return null;
}

/** Return true if `python -m mclsp --version` exits successfully. */
async function isMclspInstalled(python) {
  try {
    await execFileAsync(python, ['-m', 'mclsp', '--version'], { timeout: 10000 });
    return true;
  } catch (_) {
    return false;
  }
}

/** Run `python -m pip install --upgrade mclsp` inside a progress notification. */
async function installMclsp(python) {
  return vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: 'McCode: installing mclsp language server…',
      cancellable: false,
    },
    async (progress) => {
      progress.report({ message: 'running pip install mclsp' });
      try {
        await execFileAsync(python, ['-m', 'pip', 'install', '--upgrade', 'mclsp'],
          { timeout: 120_000 });
        return true;
      } catch (e) {
        vscode.window.showErrorMessage(
          `McCode: pip install mclsp failed: ${e.message}\n` +
          'You can install it manually with: pip install mclsp'
        );
        return false;
      }
    }
  );
}

/**
 * Resolve the server command and args to launch mclsp.
 *
 * Resolution order:
 *   1. mccode.serverPath setting (explicit user override)
 *   2. MCLSP_SERVER environment variable
 *   3. `mclsp` on PATH
 *   4. `<python> -m mclsp` (auto-discovered Python, auto-install if needed)
 *
 * Returns `{ command, args }` or `null` if the server cannot be found/installed.
 */
async function resolveMclspServer(context) {
  const config = vscode.workspace.getConfiguration('mccode');

  // 1. Explicit setting
  const configuredPath = (config.get('serverPath') || '').trim();
  if (configuredPath) {
    return { command: configuredPath, args: ['--stdio'] };
  }

  // 2. Environment variable
  if (process.env.MCLSP_SERVER) {
    return { command: process.env.MCLSP_SERVER, args: ['--stdio'] };
  }

  // 3. mclsp script on PATH
  if (await commandExists('mclsp')) {
    return { command: 'mclsp', args: ['--stdio'] };
  }

  // 4. Locate a Python interpreter
  const python = await findPython();
  if (!python) {
    vscode.window.showErrorMessage(
      'McCode: could not find Python 3.10+. ' +
      'Install Python and run: pip install mclsp\n' +
      'Or set mccode.serverPath to the full path of the mclsp executable.',
      'Open settings'
    ).then((choice) => {
      if (choice === 'Open settings') {
        vscode.commands.executeCommand('workbench.action.openSettings', 'mccode.serverPath');
      }
    });
    return null;
  }

  // Check / install mclsp in that Python
  if (!await isMclspInstalled(python)) {
    const choice = await vscode.window.showInformationMessage(
      'McCode: the mclsp language server is not installed.',
      { modal: false },
      'Install now',
      'Not now',
    );
    if (choice !== 'Install now') return null;
    const ok = await installMclsp(python);
    if (!ok) return null;
  }

  // Cache the resolved Python so subsequent activations skip the search.
  await context.globalState.update('mclsp.resolvedPython', python);
  return { command: python, args: ['-m', 'mclsp', '--stdio'] };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getInitializationOptions() {
  const config = vscode.workspace.getConfiguration('mccode');
  const flavor = config.get('flavor', 'auto');
  const logLevel = config.get('logLevel', 'warning');
  const opts = { logLevel };
  // Only pass an explicit flavor; 'auto' lets the server infer it.
  if (flavor !== 'auto') opts.flavor = flavor;
  return opts;
}

/** Fetch the virtual C document for *mccodeUri* from the server.
 *  Optionally pass *sourceText* so the server can parse on-demand if the
 *  document isn't cached yet (handles startup race conditions).
 *  Uses workspace/executeCommand (mclsp.getVirtualC) which pygls v2 handles natively.
 */
async function refreshVirtualC(mccodeUri, sourceText) {
  if (!client) return;
  try {
    const args = sourceText !== undefined ? [mccodeUri, sourceText] : [mccodeUri];
    const result = await client.sendRequest('workspace/executeCommand', {
      command: 'mclsp.getVirtualC',
      arguments: args,
    });
    if (result && result.content) {
      virtualCProvider.update(result.virtualUri, result.content);
    } else {
      console.warn('[mclsp] getVirtualC returned no content for', mccodeUri, result);
    }
  } catch (e) {
    console.error('[mclsp] getVirtualC failed for', mccodeUri, e);
  }
}

// ---------------------------------------------------------------------------
// Extension lifecycle
// ---------------------------------------------------------------------------

async function activate(context) {
  // Register the mccode-c:// content provider early so VS Code can open those
  // URIs even before the language server has responded.
  const providerDisposable = vscode.workspace.registerTextDocumentContentProvider(
    'mccode-c',
    virtualCProvider,
  );
  context.subscriptions.push(providerDisposable);

  // Register command: "McCode: Show Virtual C Document"
  // Registered unconditionally so VS Code always finds it, even if the server
  // hasn't started yet or failed to start.
  context.subscriptions.push(
    vscode.commands.registerCommand('mccode.showVirtualC', async () => {
      if (!client) {
        vscode.window.showErrorMessage(
          'McCode: language server is not running. Check the McCode Language Server output channel.',
          'Open settings'
        ).then((choice) => {
          if (choice === 'Open settings') {
            vscode.commands.executeCommand('workbench.action.openSettings', 'mccode.serverPath');
          }
        });
        return;
      }
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showInformationMessage('No active editor.');
        return;
      }
      const doc = editor.document;
      if (!doc.uri.path.match(/\.(instr|comp)$/i)) {
        vscode.window.showInformationMessage('Active file is not a McCode .instr or .comp file.');
        return;
      }
      // Fetch directly via executeCommand — avoids TextDocumentContentProvider
      // caching issues where VS Code holds a stale "loading…" copy.
      let result;
      try {
        result = await client.sendRequest('workspace/executeCommand', {
          command: 'mclsp.getVirtualC',
          arguments: [doc.uri.toString(), doc.getText()],
        });
        if (!result || !result.content) {
          vscode.window.showErrorMessage('McCode: server returned no virtual C content.');
          return;
        }
      } catch (e) {
        vscode.window.showErrorMessage(`McCode: failed to get virtual C document: ${e.message}`);
        return;
      }
      // Open the real temp .c file if available (lets clangd analyse it and
      // report diagnostics back to the McCode file via #line directives).
      // Fall back to an untitled doc if tempPath isn't provided.
      if (result.tempPath) {
        const fileUri = vscode.Uri.file(result.tempPath);
        const vdoc = await vscode.workspace.openTextDocument(fileUri);
        await vscode.window.showTextDocument(vdoc, { preview: true, viewColumn: vscode.ViewColumn.Beside });
      } else {
        const vdoc = await vscode.workspace.openTextDocument({ content: result.content, language: 'c' });
        await vscode.window.showTextDocument(vdoc, { preview: true, viewColumn: vscode.ViewColumn.Beside });
      }
    }),
  );

  // Register command: "McCode: Reinstall language server"
  context.subscriptions.push(
    vscode.commands.registerCommand('mccode.reinstallServer', async () => {
      const python = context.globalState.get('mclsp.resolvedPython') || await findPython();
      if (!python) {
        vscode.window.showErrorMessage('McCode: no Python interpreter found.');
        return;
      }
      const ok = await installMclsp(python);
      if (ok) {
        vscode.window.showInformationMessage(
          'McCode: mclsp installed. Reload window to apply.',
          'Reload'
        ).then((choice) => {
          if (choice === 'Reload') vscode.commands.executeCommand('workbench.action.reloadWindow');
        });
      }
    }),
  );

  // Resolve the server command (auto-install if needed); bail if unavailable.
  const server = await resolveMclspServer(context);
  if (!server) return;

  const serverOptions = {
    command: server.command,
    args: server.args,
    transport: TransportKind.stdio,
  };

  const clientOptions = {
    documentSelector: [
      { scheme: 'file', language: 'mccode' },
    ],
    synchronize: {
      fileEvents: vscode.workspace.createFileSystemWatcher('**/*.{instr,comp}'),
      configurationSection: 'mccode',
    },
    initializationOptions: getInitializationOptions(),
    outputChannelName: 'McCode Language Server',
  };

  client = new LanguageClient('mclsp', 'McCode Language Server', serverOptions, clientOptions);

  // Listen for server-push virtual C notifications.  The server calls
  // server.protocol.notify('$/mclsp/virtualCDocumentContent', {...}) after
  // every successful build so the cache stays warm without any polling.
  // tempPath is a real filesystem .c file written for clangd to analyse.
  client.onNotification('$/mclsp/virtualCDocumentContent', (params) => {
    if (params && params.virtualUri && params.content) {
      virtualCProvider.update(params.virtualUri, params.content);
    }
    // The temp file is already written by the server; nothing extra needed here.
  });

  // In vscode-languageclient v9, start() returns a Promise that resolves when
  // the server is ready.  Register event hooks before start() so we don't miss
  // documents that open during initialisation; refreshVirtualC already guards
  // against a not-yet-ready server with try/catch.

  // Refresh virtual C when a McCode document is opened.
  context.subscriptions.push(
    vscode.workspace.onDidOpenTextDocument(async (doc) => {
      if (doc.languageId === 'mccode' || doc.uri.path.match(/\.(instr|comp)$/i)) {
        await refreshVirtualC(doc.uri.toString(), doc.getText());
      }
    }),
  );

  // Refresh virtual C when a McCode document changes (debounced: 500 ms).
  let debounceTimer;
  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument(async (event) => {
      const doc = event.document;
      if (doc.languageId !== 'mccode' && !doc.uri.path.match(/\.(instr|comp)$/i)) return;
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(async () => {
        await refreshVirtualC(doc.uri.toString(), doc.getText());
      }, 500);
    }),
  );

  // Clean up virtual C cache when a McCode document is closed.
  context.subscriptions.push(
    vscode.workspace.onDidCloseTextDocument((doc) => {
      if (doc.languageId === 'mccode' || doc.uri.path.match(/\.(instr|comp)$/i)) {
        const virtualUri = 'mccode-c://' + doc.uri.path + '.c';
        virtualCProvider.remove(virtualUri);
      }
    }),
  );

  // Register command: "McCode: Show Virtual C Document"
  // (moved to top of activate — see above)

  // Register command: "McCode: Reinstall language server"
  // (moved to top of activate — see above)

  client.start().then(() => {
    // Refresh all already-open McCode documents now that the server is ready.
    vscode.workspace.textDocuments.forEach(async (doc) => {
      if (doc.languageId === 'mccode' || doc.uri.path.match(/\.(instr|comp)$/i)) {
        await refreshVirtualC(doc.uri.toString(), doc.getText());
      }
    });
  });
}

function deactivate() {
  if (client) {
    return client.stop();
  }
}

module.exports = { activate, deactivate };

