import SwiftUI

/// Gauge de uso: barra fina SÓ quando há gasto E teto (verde → âmbar → vermelho).
/// Sem teto, o valor aparece em texto — porcentagem sem denominador não existe.
struct UsageGaugeView: View {
    let usage: Usage

    var body: some View {
        if let fraction = usage.fraction {
            HStack(spacing: 8) {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(.quaternary)
                        Capsule()
                            .fill(barColor(for: fraction))
                            .frame(width: max(geo.size.width * fraction, 3))
                    }
                }
                .frame(height: 3)

                Text("\(Formatters.usd(usage.spentUSD)) / \(Formatters.usd(usage.capUSD))")
                    .font(.system(size: 9.5).monospacedDigit())
                    .foregroundStyle(.secondary)
                    .fixedSize()
            }
            .padding(.top, 2)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(
                "Uso: \(Int(fraction * 100)) por cento do orçamento do agente — \(Formatters.usd(usage.spentUSD)) de \(Formatters.usd(usage.capUSD))"
            )
        } else if usage.spentUSD != nil {
            Text("gasto: \(Formatters.usd(usage.spentUSD)) (sem teto)")
                .font(.system(size: 9.5).monospacedDigit())
                .foregroundStyle(.secondary)
                .accessibilityLabel("Gasto de \(Formatters.usd(usage.spentUSD)), sem orçamento do agente definido")
        }
        // Sem dado nenhum → não mostra nada (o custo já aparece como — nos metadados).
    }

    private func barColor(for fraction: Double) -> Color {
        switch fraction {
        case ..<0.6: .green
        case ..<0.85: .orange
        default: .red
        }
    }
}
