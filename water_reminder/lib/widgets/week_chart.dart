import 'package:flutter/material.dart';

import '../services/arabic.dart';
import '../theme.dart';

/// مخطط أعمدة بسيط لاستهلاك الأيام الأخيرة مع خط الهدف.
class WeekChart extends StatelessWidget {
  final List<MapEntry<DateTime, int>> days;
  final int goal;
  const WeekChart({super.key, required this.days, required this.goal});

  @override
  Widget build(BuildContext context) {
    final maxV = days.fold<int>(goal, (a, e) => e.value > a ? e.value : a) * 1.15;
    return SizedBox(
      height: 200,
      child: LayoutBuilder(builder: (context, c) {
        const labelH = 34.0;
        final chartH = c.maxHeight - labelH;
        final goalY = chartH - (goal / maxV) * chartH;
        return Stack(children: [
          Positioned(
            top: goalY,
            left: 0,
            right: 0,
            child: Row(children: [
              Expanded(
                child: Container(
                    height: 1.5, color: AppColors.accent.withValues(alpha: 0.6)),
              ),
              const SizedBox(width: 4),
              const Text('الهدف',
                  style: TextStyle(fontSize: 10, color: AppColors.accent)),
            ]),
          ),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: days.map((e) {
              final h = (e.value / maxV) * chartH;
              final met = e.value >= goal;
              return Expanded(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    TweenAnimationBuilder<double>(
                      tween: Tween(begin: 0, end: h),
                      duration: const Duration(milliseconds: 700),
                      curve: Curves.easeOutCubic,
                      builder: (_, v, __) => Container(
                        width: 22,
                        height: v < 4 ? 4 : v,
                        decoration: BoxDecoration(
                          borderRadius: BorderRadius.circular(8),
                          gradient: LinearGradient(
                            begin: Alignment.bottomCenter,
                            end: Alignment.topCenter,
                            colors: met
                                ? [AppColors.primary, AppColors.water]
                                : [
                                    AppColors.primary.withValues(alpha: 0.35),
                                    AppColors.water.withValues(alpha: 0.35)
                                  ],
                          ),
                        ),
                      ),
                    ),
                    SizedBox(
                      height: labelH,
                      child: Center(
                        child: Text(Ar.weekdaysShort[e.key.weekday - 1],
                            style: const TextStyle(
                                fontSize: 11, color: AppColors.muted)),
                      ),
                    ),
                  ],
                ),
              );
            }).toList(),
          ),
        ]);
      }),
    );
  }
}
