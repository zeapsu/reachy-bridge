import type { ExpressionsState } from "../hooks/useExpressions.ts";
import { MOVE_DATASETS, chipsDisabled, expressionHint } from "../logic/expressions.ts";

interface Props {
  expr: ExpressionsState;
  daemonOnline: boolean;
  controlBusy: boolean;
  appActive: boolean;
}

export function ExpressionsPanel({ expr, daemonOnline, controlBusy, appActive }: Props) {
  const disabled = chipsDisabled({ daemonOnline, controlBusy, appActive, exprBusy: expr.busy !== null });
  const hint = expressionHint({ daemonOnline, loaded: expr.loaded, appActive, busy: expr.busy, error: expr.error });

  return (
    <section className="glass pad instrument">
      <div className="head">
        <h2>expressions</h2>
        <div className="rule"></div>
        <button
          className="btn stop expr-cancel"
          id="expr-cancel"
          type="button"
          hidden={expr.cancelHidden}
          disabled={expr.cancelDisabled}
          onClick={expr.cancel}
        >
          cancel
        </button>
      </div>
      {MOVE_DATASETS.map(({ id, label, dataset }) => (
        <div className="expr-group" key={id}>
          <span className="expr-label">{label}</span>
          <div className="expr-strip" id={id} data-dataset={dataset} aria-live="polite">
            {(expr.moves[id] ?? []).map((move, index) => {
              const playing =
                expr.playing !== null &&
                expr.playing.move === move &&
                (expr.playing.dataset === null || expr.playing.dataset === dataset);
              return (
                <button
                  key={`${index}:${move}`}
                  className={`chip${playing ? " playing" : ""}`}
                  type="button"
                  data-move={move}
                  disabled={disabled}
                  onClick={() => {
                    if (!disabled && expr.busy === null) expr.play(dataset, move);
                  }}
                >
                  {move}
                </button>
              );
            })}
          </div>
        </div>
      ))}
      <p className={`panel-hint${hint.error ? " error" : ""}`} id="expr-hint" hidden={hint.hidden}>
        {hint.text}
      </p>
    </section>
  );
}
