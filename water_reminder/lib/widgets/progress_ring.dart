import 'dart:math';

import 'package:flutter/material.dart';

import '../theme.dart';

/// حلقة تقدم دائرية متحركة مع تدرّج لوني مائي.
class ProgressRing extends StatelessWidget {
  final double progress;
  final double size;
  final Widget child;
  const ProgressRing(
      {super.key, required this.progress, this.size = 230, required this.child});

  @override
  Widget build(BuildContext context) {
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0, end: progress),
      duration: const Duration(milliseconds: 900),
      curve: Curves.easeOutCubic,
      builder: (context, v, _) => SizedBox(
        width: size,
        height: size,
        child: CustomPaint(
          painter: _RingPainter(v),
          child: Center(child: child),
        ),
      ),
    );
  }
}

class _RingPainter extends CustomPainter {
  final double value;
  _RingPainter(this.value);

  @override
  void paint(Canvas canvas, Size size) {
    const stroke = 18.0;
    final center = size.center(Offset.zero);
    final radius = (size.width - stroke) / 2;
    final rect = Rect.fromCircle(center: center, radius: radius);

    canvas.drawCircle(
        center,
        radius,
        Paint()
          ..color = AppColors.mint
          ..style = PaintingStyle.stroke
          ..strokeWidth = stroke);

    // تعبئة داخلية خفيفة بمستوى الماء
    final inner = radius - stroke;
    canvas.save();
    canvas.clipPath(Path()..addOval(Rect.fromCircle(center: center, radius: inner)));
    final level = center.dy + inner - (2 * inner * value);
    canvas.drawRect(
        Rect.fromLTRB(0, level, size.width, size.height),
        Paint()..color = AppColors.water.withValues(alpha: 0.12));
    canvas.restore();

    if (value <= 0) return;
    final paint = Paint()
      ..shader = const SweepGradient(
        startAngle: -pi / 2,
        endAngle: 3 * pi / 2,
        colors: [AppColors.water, AppColors.primary, AppColors.water],
      ).createShader(rect)
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeWidth = stroke;
    canvas.drawArc(rect, -pi / 2, 2 * pi * value, false, paint);
  }

  @override
  bool shouldRepaint(_RingPainter old) => old.value != value;
}
