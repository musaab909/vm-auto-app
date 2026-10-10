import 'package:flutter/material.dart';

import '../services/app_state.dart';
import '../services/arabic.dart';
import '../theme.dart';
import '../widgets/common.dart';
import '../widgets/progress_ring.dart';

class DashboardScreen extends StatelessWidget {
  final AppState state;
  const DashboardScreen({super.key, required this.state});

  String _greeting() {
    final h = DateTime.now().hour;
    if (h < 12) return 'صباح الخير';
    return 'مساء الخير';
  }

  String _motivation(double p) {
    if (p >= 1) return 'رائع! حققت هدفك اليوم 🎉';
    if (p >= 0.75) return 'أوشكت على الوصول، استمر! 💪';
    if (p >= 0.5) return 'تجاوزت النصف، أداء ممتاز 👏';
    if (p > 0) return 'بداية جيدة، واصل الشرب بانتظام 💧';
    return 'ابدأ يومك بكوب من الماء ☀️';
  }

  Future<void> _custom(BuildContext context) async {
    final ctrl = TextEditingController();
    final ml = await showDialog<int>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('إضافة كمية مخصصة'),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          keyboardType: TextInputType.number,
          decoration: const InputDecoration(
              labelText: 'الكمية بالمليلتر', suffixText: 'مل'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('إلغاء')),
          FilledButton(
            onPressed: () {
              final v = int.tryParse(_toLatin(ctrl.text.trim()));
              if (v == null || v <= 0 || v > 2000) {
                showMessage(ctx, 'الرجاء إدخال كمية بين ١ و٢٠٠٠ مل');
                return;
              }
              Navigator.pop(ctx, v);
            },
            child: const Text('إضافة'),
          ),
        ],
      ),
    );
    if (ml != null && context.mounted) await _add(context, ml);
  }

  static String _toLatin(String s) {
    const ar = '٠١٢٣٤٥٦٧٨٩';
    return s.split('').map((c) {
      final i = ar.indexOf(c);
      return i >= 0 ? '$i' : c;
    }).join();
  }

  Future<void> _add(BuildContext context, int ml) async {
    final before = state.todayTotal;
    await state.addWater(ml);
    if (!context.mounted) return;
    if (before < state.goal && state.todayTotal >= state.goal) {
      showMessage(context, 'تهانينا! لقد حققت هدفك اليومي من الماء 🎉');
    } else {
      showMessage(context, 'تمت إضافة ${Ar.ml(ml)} 💧');
    }
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: state,
      builder: (context, _) {
        final p = state.profile!;
        final remaining = (state.goal - state.todayTotal).clamp(0, state.goal);
        return SafeArea(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(18, 12, 18, 24),
            children: [
              Row(children: [
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('${_greeting()}، ${p.name.split(' ').first}',
                        style: const TextStyle(
                            fontSize: 22,
                            fontWeight: FontWeight.bold,
                            color: AppColors.text)),
                    Text('${Ar.date(DateTime.now())} • ${p.grade}',
                        style: const TextStyle(color: AppColors.muted)),
                  ]),
                ),
                if (state.streak > 0)
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                    decoration: BoxDecoration(
                      color: AppColors.accent.withValues(alpha: 0.12),
                      borderRadius: BorderRadius.circular(14),
                    ),
                    child: Row(children: [
                      const Icon(Icons.local_fire_department,
                          color: AppColors.accent, size: 18),
                      Text(' ${Ar.n(state.streak)}',
                          style: const TextStyle(
                              color: AppColors.accent, fontWeight: FontWeight.bold)),
                    ]),
                  ),
              ]),
              const SizedBox(height: 18),
              SectionCard(
                padding: const EdgeInsets.symmetric(vertical: 24, horizontal: 16),
                child: Column(children: [
                  ProgressRing(
                    progress: state.progress,
                    child: Column(mainAxisSize: MainAxisSize.min, children: [
                      const Icon(Icons.water_drop, color: AppColors.water, size: 30),
                      Text('${Ar.n((state.progress * 100).round())}٪',
                          style: const TextStyle(
                              fontSize: 38,
                              fontWeight: FontWeight.bold,
                              color: AppColors.text)),
                      Text('${Ar.n(state.todayTotal)} / ${Ar.ml(state.goal)}',
                          style: const TextStyle(
                              color: AppColors.muted, fontWeight: FontWeight.w600)),
                    ]),
                  ),
                  const SizedBox(height: 16),
                  Text(_motivation(state.progress),
                      style: const TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w600,
                          color: AppColors.primaryDark)),
                  const SizedBox(height: 4),
                  Text(
                      remaining > 0
                          ? 'متبقٍ لك ${Ar.ml(remaining)} لبلوغ هدفك'
                          : 'أحسنت! حافظ على هذا المستوى',
                      style: const TextStyle(color: AppColors.muted)),
                ]),
              ),
              const SizedBox(height: 18),
              const Text('إضافة سريعة',
                  style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
              const SizedBox(height: 10),
              Row(children: [
                _QuickButton(
                    ml: 150, icon: Icons.local_cafe_outlined, onTap: () => _add(context, 150)),
                const SizedBox(width: 10),
                _QuickButton(
                    ml: 250, icon: Icons.local_drink_outlined, onTap: () => _add(context, 250)),
                const SizedBox(width: 10),
                _QuickButton(
                    ml: 500, icon: Icons.water_outlined, onTap: () => _add(context, 500)),
              ]),
              const SizedBox(height: 10),
              Row(children: [
                Expanded(
                  child: OutlinedButton.icon(
                    onPressed: () => _custom(context),
                    icon: const Icon(Icons.edit_outlined),
                    label: const Text('كمية مخصصة'),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: OutlinedButton.icon(
                    onPressed: state.todayEntries.isEmpty
                        ? null
                        : () async {
                            await state.undoLast();
                            if (context.mounted) {
                              showMessage(context, 'تم التراجع عن آخر إضافة');
                            }
                          },
                    icon: const Icon(Icons.undo),
                    label: const Text('تراجع'),
                  ),
                ),
              ]),
              const SizedBox(height: 18),
              const Text('سجل اليوم',
                  style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
              const SizedBox(height: 10),
              if (state.todayEntries.isEmpty)
                const SectionCard(
                  child: Row(children: [
                    IconBadge(Icons.hourglass_empty),
                    SizedBox(width: 12),
                    Expanded(
                        child: Text('لم تسجّل أي كمية اليوم بعد.',
                            style: TextStyle(color: AppColors.muted))),
                  ]),
                )
              else
                SectionCard(
                  padding: const EdgeInsets.symmetric(vertical: 6),
                  child: Column(
                    children: state.todayEntries
                        .take(12)
                        .map((e) => ListTile(
                              leading: const IconBadge(Icons.water_drop,
                                  color: AppColors.water),
                              title: Text(Ar.ml(e.ml),
                                  style: const TextStyle(fontWeight: FontWeight.bold)),
                              trailing: Text(Ar.time(e.time),
                                  style: const TextStyle(color: AppColors.muted)),
                            ))
                        .toList(),
                  ),
                ),
            ],
          ),
        );
      },
    );
  }
}

class _QuickButton extends StatelessWidget {
  final int ml;
  final IconData icon;
  final VoidCallback onTap;
  const _QuickButton({required this.ml, required this.icon, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Material(
        color: Colors.white,
        borderRadius: BorderRadius.circular(20),
        child: InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: onTap,
          child: Container(
            padding: const EdgeInsets.symmetric(vertical: 16),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(20),
              border: Border.all(color: AppColors.primary.withValues(alpha: 0.15)),
            ),
            child: Column(children: [
              Icon(icon, color: AppColors.primary, size: 28),
              const SizedBox(height: 6),
              Text('+${Ar.n(ml)}',
                  style: const TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.bold,
                      color: AppColors.text)),
              const Text('مل', style: TextStyle(color: AppColors.muted, fontSize: 12)),
            ]),
          ),
        ),
      ),
    );
  }
}
