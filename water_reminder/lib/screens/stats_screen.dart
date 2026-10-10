import 'package:flutter/material.dart';

import '../services/app_state.dart';
import '../services/arabic.dart';
import '../services/hydration.dart';
import '../theme.dart';
import '../widgets/common.dart';
import '../widgets/week_chart.dart';

class StatsScreen extends StatelessWidget {
  final AppState state;
  const StatsScreen({super.key, required this.state});

  static const tips = [
    (Icons.wb_sunny_outlined, 'ابدأ يومك بكوب ماء',
        'اشرب كوباً (٢٠٠–٢٥٠ مل) بعد الاستيقاظ لتعويض ما فقده جسمك أثناء النوم.'),
    (Icons.backpack_outlined, 'احمل قارورتك معك',
        'قارورة ماء قابلة لإعادة الاستخدام في حقيبتك تجعل الشرب سهلاً طوال الدوام.'),
    (Icons.schedule, 'وزّع الشرب على اليوم',
        'رشفات منتظمة بين الحصص أفضل من كمية كبيرة دفعة واحدة؛ كوب صغير كل ساعة تقريباً.'),
    (Icons.sports_soccer_outlined, 'قبل الرياضة وبعدها',
        'اشرب قبل حصة التربية البدنية بنصف ساعة، ثم كوباً أو كوبين بعدها لتعويض العرق.'),
    (Icons.psychology_outlined, 'الماء يقوّي التركيز',
        'نقص الماء ولو بنسبة بسيطة قد يسبب الصداع والتعب وضعف الانتباه في الحصة.'),
    (Icons.water_drop_outlined, 'راقب لون البول',
        'اللون الأصفر الفاتح علامة على ترطيب جيد، أما الداكن فيعني أنك تحتاج إلى مزيد من الماء.'),
    (Icons.no_drinks_outlined, 'قلّل المشروبات السكرية',
        'العصائر المحلاة والمشروبات الغازية لا تغني عن الماء، وتضر بالأسنان والوزن.'),
    (Icons.thermostat_outlined, 'في الأيام الحارة',
        'زِد كميتك عند ارتفاع الحرارة أو بعد اللعب في الساحة، ولا تنتظر الشعور بالعطش.'),
    (Icons.nightlight_outlined, 'خفّف قبل النوم',
        'اشرب أكثر في النهار وقلّل الكمية في الساعة الأخيرة قبل النوم لتنعم بنوم هادئ.'),
  ];

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: state,
      builder: (context, _) {
        final week = state.lastDays(7);
        final weekTotal = week.fold<int>(0, (a, e) => a + e.value);
        final activeDays = week.where((e) => e.value > 0).length;
        final avg = activeDays == 0 ? 0 : weekTotal ~/ activeDays;
        final history = state.history.entries.where((e) => e.value > 0).toList()
          ..sort((a, b) => b.key.compareTo(a.key));
        final school = Hydration.schoolShare(state.goal);

        return SafeArea(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(18, 12, 18, 24),
            children: [
              const Text('الإحصائيات والأداء',
                  style: TextStyle(
                      fontSize: 22, fontWeight: FontWeight.bold, color: AppColors.text)),
              const SizedBox(height: 16),
              GridView.count(
                crossAxisCount: 2,
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                mainAxisSpacing: 10,
                crossAxisSpacing: 10,
                childAspectRatio: 1.45,
                children: [
                  StatTile(
                      icon: Icons.local_fire_department,
                      color: AppColors.accent,
                      value: Ar.days(state.streak),
                      label: 'سلسلة الأيام المتتالية'),
                  StatTile(
                      icon: Icons.emoji_events_outlined,
                      color: const Color(0xFFF2B630),
                      value: Ar.days(state.daysGoalMet),
                      label: 'أيام تحقق فيها الهدف'),
                  StatTile(
                      icon: Icons.show_chart,
                      value: Ar.ml(avg),
                      label: 'المتوسط اليومي هذا الأسبوع'),
                  StatTile(
                      icon: Icons.star_outline,
                      color: AppColors.water,
                      value: Ar.ml(state.bestDay),
                      label: 'أفضل يوم مسجّل'),
                ],
              ),
              const SizedBox(height: 16),
              SectionCard(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Text('معدل الاستهلاك الأسبوعي',
                      style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
                  Text('إجمالي الأيام السبعة الأخيرة: ${Ar.liters(weekTotal)}',
                      style: const TextStyle(color: AppColors.muted)),
                  const SizedBox(height: 16),
                  WeekChart(days: week, goal: state.goal),
                ]),
              ),
              const SizedBox(height: 16),
              SectionCard(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Row(children: [
                    IconBadge(Icons.school_outlined),
                    SizedBox(width: 10),
                    Text('خطة توزيع اليوم الدراسي',
                        style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
                  ]),
                  const SizedBox(height: 12),
                  _planRow('قبل الذهاب إلى المدرسة', (state.goal * 0.15).round()),
                  _planRow('خلال الحصص الأولى', (school * 0.4).round()),
                  _planRow('في الاستراحة', (school * 0.3).round()),
                  _planRow('خلال الحصص الأخيرة', (school * 0.3).round()),
                  _planRow('بعد العودة إلى المنزل وحتى المساء',
                      state.goal - (state.goal * 0.15).round() - school),
                ]),
              ),
              const SizedBox(height: 16),
              const Text('أفضل الممارسات لشرب الماء',
                  style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
              const SizedBox(height: 10),
              ...tips.map((t) => Padding(
                    padding: const EdgeInsets.only(bottom: 10),
                    child: SectionCard(
                      padding: const EdgeInsets.all(14),
                      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        IconBadge(t.$1),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(t.$2,
                                    style: const TextStyle(fontWeight: FontWeight.bold)),
                                const SizedBox(height: 4),
                                Text(t.$3,
                                    style: const TextStyle(
                                        color: AppColors.muted, height: 1.5)),
                              ]),
                        ),
                      ]),
                    ),
                  )),
              const SizedBox(height: 6),
              const Text('سجل الأيام السابقة',
                  style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
              const SizedBox(height: 10),
              if (history.isEmpty)
                const SectionCard(
                    child: Text('لا يوجد سجل بعد. ابدأ بإضافة الماء من الشاشة الرئيسية.',
                        style: TextStyle(color: AppColors.muted)))
              else
                SectionCard(
                  padding: const EdgeInsets.symmetric(vertical: 6),
                  child: Column(
                    children: history.take(30).map((e) {
                      final parts = e.key.split('-').map(int.parse).toList();
                      final d = DateTime(parts[0], parts[1], parts[2]);
                      final met = e.value >= state.goal;
                      final ratio = (e.value / state.goal).clamp(0.0, 1.0);
                      return ListTile(
                        leading: Icon(
                            met ? Icons.check_circle : Icons.radio_button_unchecked,
                            color: met ? AppColors.primary : AppColors.muted),
                        title: Text(Ar.date(d)),
                        subtitle: Padding(
                          padding: const EdgeInsets.only(top: 6),
                          child: ClipRRect(
                            borderRadius: BorderRadius.circular(6),
                            child: LinearProgressIndicator(
                              value: ratio,
                              minHeight: 6,
                              backgroundColor: AppColors.mint,
                            ),
                          ),
                        ),
                        trailing: Text(Ar.ml(e.value),
                            style: const TextStyle(fontWeight: FontWeight.bold)),
                      );
                    }).toList(),
                  ),
                ),
            ],
          ),
        );
      },
    );
  }

  Widget _planRow(String label, int ml) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 5),
        child: Row(children: [
          const Icon(Icons.circle, size: 8, color: AppColors.water),
          const SizedBox(width: 8),
          Expanded(child: Text(label)),
          Text(Ar.ml((ml / 10).round() * 10),
              style: const TextStyle(
                  fontWeight: FontWeight.bold, color: AppColors.primaryDark)),
        ]),
      );
}
