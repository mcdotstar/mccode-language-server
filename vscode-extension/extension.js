// McCode VS Code extension – launches mclsp as a stdio language server.
// Requires `mclsp` to be installed and on PATH (pip install mclsp).

const vscode = require('vscode');
const { LanguageClient, TransportKind } = require('vscode-languageclient/node');

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

function activate(context) {
  // Register the mccode-c:// content provider early so VS Code can open those
  // URIs even before the language server has responded.
  const providerDisposable = vscode.workspace.registerTextDocumentContentProvider(
    'mccode-c',
    virtualCProvider,
  );
  context.subscriptions.push(providerDisposable);

  const config = vscode.workspace.getConfiguration('mccode');
  const mclspCommand = config.get('serverPath') || process.env.MCLSP_SERVER || 'mclsp';

  const serverOptions = {
    command: mclspCommand,
    args: ['--stdio'],
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
  context.subscriptions.push(
    vscode.commands.registerCommand('mccode.showVirtualC', async () => {
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

