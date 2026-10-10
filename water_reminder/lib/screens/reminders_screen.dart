import 'package:flutter/material.dart';

import '../services/app_state.dart';
import '../services/arabic.dart';
import '../services/hydration.dart';
import '../services/notifications.dart';
import '../theme.dart';
import '../widgets/common.dart';

class RemindersScreen extends StatelessWidget {
  final AppState state;
  const RemindersScreen({super.key, required this.state});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: state,
      builder: (context, _) {
        final auto = Hydration.autoIntervalMinutes(state.goal);
        final now = DateTime.now();
        final next = now.add(Duration(minutes: state.intervalMinutes));
        final perReminder = (state.goal / (14 * 60 / state.intervalMinutes)).round();
        return SafeArea(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(18, 12, 18, 24),
            children: [
              const Text('التذكير والمؤقت الذكي',
                  style: TextStyle(
                      fontSize: 22, fontWeight: FontWeight.bold, color: AppColors.text)),
              const SizedBox(height: 16),
              Container(
                padding: const EdgeInsets.all(20),
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    colors: state.remindersOn
                        ? [AppColors.primary, AppColors.water]
                        : [Colors.blueGrey.shade300, Colors.blueGrey.shade400],
                  ),
                  borderRadius: BorderRadius.circular(24),
                ),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Row(children: [
                    Icon(
                        state.remindersOn
                            ? Icons.notifications_active
                            : Icons.notifications_off,
                        color: Colors.white,
                        size: 30),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                          state.remindersOn ? 'التذكيرات مفعّلة' : 'التذكيرات متوقفة',
                          style: const TextStyle(
                              color: Colors.white,
                              fontSize: 19,
                              fontWeight: FontWeight.bold)),
                    ),
                    Switch(
                      value: state.remindersOn,
                      activeThumbColor: Colors.white,
                      onChanged: (v) async {
                        if (v) {
                          await NotificationService.instance
                              .requestPermission()
                              .catchError((_) => false);
                        }
                        await state.setReminders(on: v);
                      },
                    ),
                  ]),
                  const SizedBox(height: 12),
                  Text(
                      state.remindersOn
                          ? 'سنذكّرك كل ${Ar.minutes(state.intervalMinutes)}\nالتذكير القادم تقريباً: ${Ar.time(next)}'
                          : 'فعّل التذكيرات ليصلك تنبيه دوري بشرب الماء حتى دون إنترنت.',
                      style: const TextStyle(color: Colors.white, height: 1.6)),
                ]),
              ),
              const SizedBox(height: 16),
              SectionCard(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    secondary: const IconBadge(Icons.auto_awesome),
                    title: const Text('الفترة الذكية التلقائية',
                        style: TextStyle(fontWeight: FontWeight.bold)),
                    subtitle: Text(
                        'محسوبة من وزنك وعمرك: كل ${Ar.minutes(auto)}'),
                    value: state.autoInterval,
                    onChanged: (v) => state.setReminders(auto: v),
                  ),
                  if (!state.autoInterval) ...[
                    const Divider(),
                    Row(children: [
                      const Text('فترة مخصصة',
                          style: TextStyle(fontWeight: FontWeight.w600)),
                      const Spacer(),
                      Text(Ar.minutes(state.customInterval),
                          style: const TextStyle(
                              color: AppColors.primaryDark,
                              fontWeight: FontWeight.bold)),
                    ]),
                    Slider(
                      value: state.customInterval.toDouble(),
                      min: 15,
                      max: 180,
                      divisions: 33,
                      label: Ar.minutes(state.customInterval),
                      onChanged: (v) => state.setReminders(interval: v.round()),
                    ),
                    Wrap(
                      spacing: 8,
                      children: [30, 45, 60, 90, 120]
                          .map((m) => ChoiceChip(
                                label: Text(Ar.minutes(m)),
                                selected: state.customInterval == m,
                                onSelected: (_) => state.setReminders(interval: m),
                              ))
                          .toList(),
                    ),
                  ],
                ]),
              ),
              const SizedBox(height: 16),
              SectionCard(
                child: Row(children: [
                  const IconBadge(Icons.local_drink, color: AppColors.water),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(
                        'اشرب نحو ${Ar.ml((perReminder / 10).round() * 10)} عند كل تذكير لتبلغ هدفك اليومي (${Ar.ml(state.goal)}).',
                        style: const TextStyle(height: 1.5)),
                  ),
                ]),
              ),
              const SizedBox(height: 16),
              SectionCard(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Row(children: [
                    IconBadge(Icons.music_note_outlined),
                    SizedBox(width: 10),
                    Text('نغمة التنبيه',
                        style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                  ]),
                  const SizedBox(height: 6),
                  RadioGroup<ReminderTone>(
                    groupValue: state.tone,
                    onChanged: (v) => state.setReminders(newTone: v),
                    child: Column(
                      children: ReminderTone.values
                          .map((t) => RadioListTile<ReminderTone>(
                                contentPadding: EdgeInsets.zero,
                                value: t,
                                title: Text(t.label),
                              ))
                          .toList(),
                    ),
                  ),
                  OutlinedButton.icon(
                    onPressed: () async {
                      await NotificationService.instance
                          .requestPermission()
                          .catchError((_) => false);
                      await NotificationService.instance.showTest(state.tone);
                    },
                    icon: const Icon(Icons.play_arrow),
                    label: const Text('تجربة التنبيه الآن'),
                  ),
                ]),
              ),
            ],
          ),
        );
      },
    );
  }
}
