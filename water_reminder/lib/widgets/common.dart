import 'package:flutter/material.dart';

import '../theme.dart';

class SectionCard extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  const SectionCard(
      {super.key, required this.child, this.padding = const EdgeInsets.all(18)});

  @override
  Widget build(BuildContext context) =>
      Card(child: Padding(padding: padding, child: child));
}

class IconBadge extends StatelessWidget {
  final IconData icon;
  final Color color;
  const IconBadge(this.icon, {super.key, this.color = AppColors.primary});

  @override
  Widget build(BuildContext context) => Container(
        width: 42,
        height: 42,
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.12),
          borderRadius: BorderRadius.circular(14),
        ),
        child: Icon(icon, color: color, size: 22),
      );
}

class StatTile extends StatelessWidget {
  final IconData icon;
  final String value;
  final String label;
  final Color color;
  const StatTile(
      {super.key,
      required this.icon,
      required this.value,
      required this.label,
      this.color = AppColors.primary});

  @override
  Widget build(BuildContext context) => SectionCard(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            IconBadge(icon, color: color),
            const SizedBox(height: 10),
            Text(value,
                style: const TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.bold,
                    color: AppColors.text)),
            Text(label,
                style: const TextStyle(fontSize: 12, color: AppColors.muted)),
          ],
        ),
      );
}

void showMessage(BuildContext context, String text) {
  ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(
      content: Text(text),
      behavior: SnackBarBehavior.floating,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    ));
}
