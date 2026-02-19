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
  // Only pass an explicit flavor; 'auto' lets the server infer it.
  return flavor !== 'auto' ? { flavor } : {};
}

/** Fetch the virtual C document for *mccodeUri* from the server. */
async function refreshVirtualC(mccodeUri) {
  if (!client) return;
  try {
    const result = await client.sendRequest('$/mclsp/virtualCDocument', {
      uri: mccodeUri,
    });
    if (result && result.content) {
      virtualCProvider.update(result.virtualUri, result.content);
    }
  } catch (e) {
    // Server may not have the document yet — silently ignore.
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

  const serverOptions = {
    command: 'mclsp',
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
  client.start();

  // After the client is ready, hook document open/change events to refresh
  // the virtual C document so it stays in sync with the McCode source.
  client.onReady().then(() => {
    // Refresh virtual C when a McCode document is opened.
    context.subscriptions.push(
      vscode.workspace.onDidOpenTextDocument(async (doc) => {
        if (doc.languageId === 'mccode' || doc.uri.path.match(/\.(instr|comp)$/i)) {
          await refreshVirtualC(doc.uri.toString());
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
          await refreshVirtualC(doc.uri.toString());
        }, 500);
      }),
    );

    // Clean up virtual C cache when a McCode document is closed.
    context.subscriptions.push(
      vscode.workspace.onDidCloseTextDocument((doc) => {
        if (doc.languageId === 'mccode' || doc.uri.path.match(/\.(instr|comp)$/i)) {
          // Derive virtual URI the same way the server does.
          const virtualUri = 'mccode-c://' + doc.uri.path + '.c';
          virtualCProvider.remove(virtualUri);
        }
      }),
    );

    // Refresh all already-open McCode documents (in case they were opened
    // before the server was ready).
    vscode.workspace.textDocuments.forEach(async (doc) => {
      if (doc.languageId === 'mccode' || doc.uri.path.match(/\.(instr|comp)$/i)) {
        await refreshVirtualC(doc.uri.toString());
      }
    });
  });

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
      await refreshVirtualC(doc.uri.toString());
      const virtualUri = vscode.Uri.parse('mccode-c://' + doc.uri.path + '.c');
      const vdoc = await vscode.workspace.openTextDocument(virtualUri);
      await vscode.window.showTextDocument(vdoc, { preview: true, viewColumn: vscode.ViewColumn.Beside });
    }),
  );
}

function deactivate() {
  if (client) {
    return client.stop();
  }
}

module.exports = { activate, deactivate };

