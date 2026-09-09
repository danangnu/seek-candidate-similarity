-- Local test copy only. Run this file in HeidiSQL before the updated scraper.
USE `seek_uuid_test_trackitlive`;

CREATE TABLE IF NOT EXISTS `seek_scrap_settings` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `send_email` tinyint(4) NOT NULL DEFAULT '1',
  `email_to` text,
  `cc` tinyint(4) NOT NULL DEFAULT '1',
  `email_cc` text,
  `error_cc` tinyint(4) NOT NULL DEFAULT '1',
  `email_error_cc` text,
  `email_subject` varchar(255) DEFAULT NULL,
  `email_content` blob,
  `idle_between_runs` int(11) NOT NULL DEFAULT '0' COMMENT 'idle in minutes',
  `idle_between_runs2` int(11) NOT NULL DEFAULT '0' COMMENT 'idle in minutes',
  `otp_status` tinyint(4) DEFAULT '0',
  `otp_staff` varchar(255) DEFAULT NULL,
  `otp_time` datetime DEFAULT NULL,
  `delete_days` int(11) DEFAULT NULL,
  `ignore_days` int(11) DEFAULT NULL,
  `send_email_keyword` tinyint(4) DEFAULT NULL,
  `email_to_keyword` text,
  `cc_keyword` tinyint(4) DEFAULT NULL,
  `email_cc_keyword` text,
  `email_subject_keyword` varchar(255) DEFAULT NULL,
  `email_content_keyword` blob,
  `idle_more_than` int(11) NOT NULL DEFAULT '0',
  `idle_more_than2` int(11) NOT NULL DEFAULT '0',
  `idle_less_than` int(11) NOT NULL DEFAULT '0',
  `idle_less_than2` int(11) NOT NULL DEFAULT '0',
  `updated_by` varchar(255) DEFAULT NULL,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `is_emergency` int(11) NOT NULL DEFAULT '0',
  `last_run_check_run_missing_keyword` datetime DEFAULT NULL,
  `staff_last_run_check_run_missing_keyword` varchar(20) DEFAULT NULL,
  `seek_result_limit` int(11) DEFAULT NULL,
  `last_run_check_run_missing_keyword_report` datetime DEFAULT NULL,
  `staff_last_run_check_run_missing_keyword_report` varchar(20) DEFAULT NULL,
  `last_purge_bulk_search` datetime DEFAULT NULL,
  `last_purge_bulk_search_by` varchar(50) DEFAULT NULL,
  `last_purge_temp_exclusion` datetime DEFAULT NULL,
  `last_purge_temp_exclusion_by` varchar(50) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `send_email` (`send_email`),
  KEY `cc` (`cc`),
  KEY `email_subject` (`email_subject`),
  KEY `idle_more_than` (`idle_more_than`),
  KEY `idle_more_than2` (`idle_more_than2`),
  KEY `idle_less_than` (`idle_less_than`),
  KEY `idle_less_than2` (`idle_less_than2`),
  KEY `error_cc` (`error_cc`),
  FULLTEXT KEY `email_to` (`email_to`),
  FULLTEXT KEY `email_cc` (`email_cc`),
  FULLTEXT KEY `email_error_cc` (`email_error_cc`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

-- Populate id=1 only when it does not exist. Never overwrite existing settings.
INSERT INTO seek_scrap_settings
    (id, idle_less_than, idle_less_than2, idle_more_than, idle_more_than2)
SELECT 1, 30, 300, 5, 25
WHERE NOT EXISTS (SELECT 1 FROM seek_scrap_settings WHERE id=1);

SELECT id, idle_less_than, idle_less_than2, idle_more_than, idle_more_than2
FROM seek_scrap_settings WHERE id=1;
