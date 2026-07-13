import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * App-wide error boundary. Without one, any render-time throw unmounts the
 * whole React tree and leaves a blank screen with no way out. This catches
 * the error, logs it for diagnosis, and shows a recover-by-reloading screen
 * — sessions and data live on the server, so a reload restores the UI.
 */
export class RootErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surfaced in the console/logs so a specific crash can be diagnosed;
    // the fallback below is what the user actually sees.
    console.error("Erro não tratado na interface:", error, info.componentStack);
  }

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div
        role="alert"
        className="flex h-screen w-screen flex-col items-center justify-center gap-4 bg-background p-8 text-center text-foreground"
      >
        <div className="text-5xl" aria-hidden="true">
          🐟
        </div>
        <h1 className="text-xl font-semibold">Algo deu errado</h1>
        <p className="max-w-md text-sm text-muted-foreground">
          A interface encontrou um erro inesperado e precisou parar. Suas sessões e seus dados estão
          salvos no servidor — recarregue para continuar de onde parou.
        </p>
        <button
          type="button"
          onClick={this.handleReload}
          className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
        >
          Recarregar
        </button>
        {error.message && (
          <pre className="mt-2 max-h-40 max-w-md overflow-auto rounded-md bg-muted p-3 text-left text-xs whitespace-pre-wrap text-muted-foreground">
            {error.message}
          </pre>
        )}
      </div>
    );
  }
}
