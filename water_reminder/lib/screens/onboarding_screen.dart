import 'package:flutter/material.dart';

import '../services/app_state.dart';
import '../services/notifications.dart';
import '../theme.dart';
import '../widgets/profile_form.dart';

class OnboardingScreen extends StatelessWidget {
  final AppState state;
  const OnboardingScreen({super.key, required this.state});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(18, 24, 18, 32),
          children: [
            Center(
              child: Container(
                width: 92,
                height: 92,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  gradient: const LinearGradient(
                      colors: [AppColors.water, AppColors.primary]),
                  boxShadow: [
                    BoxShadow(
                        color: AppColors.primary.withValues(alpha: 0.3),
                        blurRadius: 24,
                        offset: const Offset(0, 10)),
                  ],
                ),
                child: const Icon(Icons.water_drop, color: Colors.white, size: 48),
              ),
            ),
            const SizedBox(height: 16),
            const Text('أهلاً بك في مذكر شرب الماء',
                textAlign: TextAlign.center,
                style: TextStyle(
                    fontSize: 24, fontWeight: FontWeight.bold, color: AppColors.text)),
            const SizedBox(height: 6),
            const Text(
                'أدخل بياناتك لنحسب احتياجك اليومي من الماء ونذكّرك بالشرب في الأوقات المناسبة.',
                textAlign: TextAlign.center,
                style: TextStyle(color: AppColors.muted, height: 1.5)),
            const SizedBox(height: 22),
            ProfileForm(
              submitLabel: 'ابدأ رحلتك مع الماء',
              onSubmit: (p) async {
                await NotificationService.instance.requestPermission().catchError((_) => false);
                await state.saveProfile(p);
              },
            ),
          ],
        ),
      ),
    );
  }
}
